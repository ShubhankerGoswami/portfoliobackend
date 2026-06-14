"""
voice_agent.py – Session-aware voice pipeline
  STT  : OpenAI Whisper (whisper-1)
  LLM  : GPT-4o mini  (with Calendly tool calling)
  TTS  : ElevenLabs Flash v2.5
  TOOL : Calendly – get availability + create booking link
"""
import sys
# Force UTF-8 stdout/stderr — prevents Windows charmap UnicodeEncodeError on non-ASCII LLM output
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import aiohttp

# Sentence boundary: .!? followed by whitespace (handles streaming splits)
_SENT_BOUND  = re.compile(r'(?<=[.!?])\s+')
_MIN_SENT    = 12   # don't TTS fragments shorter than this (chars)

# ── env ───────────────────────────────────────────────────────────────────────
OPENAI_API_KEY      = os.getenv("OPENAI_API_KEY", "")
ELEVENLABS_API_KEY  = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb")
CALENDLY_API_KEY    = os.getenv("CALENDLY_API_KEY", "")   # Personal Access Token

SESSION_TTL = int(os.getenv("VOICE_SESSION_TTL", 1800))   # 30 min inactivity

# IST = UTC + 5:30
IST = timezone(timedelta(hours=5, minutes=30))

# ── system prompt ─────────────────────────────────────────────────────────────
SYSTEM_MESSAGE = {
    "role": "system",
    "content": (
        "You are Cogni, Shubhanker Goswami's AI voice assistant — intelligent, "
        "friendly, and articulate. You help portfolio visitors learn about his "
        "experience and, when they want to connect, schedule a real meeting.\n\n"

        "=== MANDATORY FACT — CHECK THIS BEFORE EVERY REPLY ===\n"
        "Shubhanker has worked at EXACTLY TWO companies in his entire career:\n"
        "  COMPANY 1: SmartSense Consulting Solutions Pvt Ltd | AI Product Manager | Apr 2022 – Present\n"
        "  COMPANY 2: Green Power International | Engineer | Jan 2017 – Feb 2019\n\n"
        "Insight 360, Ostrich AI, Second Brain AI, and Foster are PRODUCTS he built "
        "WHILE AT SmartSense. They are NOT companies. They are NOT employers.\n"
        "WRONG — NEVER SAY: 'He worked at Insight 360' / 'His experience at Ostrich AI' / "
        "'His roles at Insight 360, Ostrich AI, and Second Brain AI'\n"
        "RIGHT — ALWAYS SAY: 'He built Insight 360 at SmartSense' / "
        "'At SmartSense he led products like Insight 360, Ostrich AI, Second Brain AI, and Foster'\n"
        "If asked how many companies he has worked at, the answer is TWO: SmartSense and Green Power International.\n"
        "=== END MANDATORY FACT ===\n\n"

        "LANGUAGE RULES:\n"
        "- Always respond in English only, regardless of the language the user speaks.\n\n"

        "VOICE FORMAT RULES:\n"
        "- Keep answers SHORT (2-4 sentences max).\n"
        "- Never use bullet points, markdown, or special characters.\n"
        "- Speak naturally as if in a real conversation.\n"
        "- When presenting meeting slots, read out at most 3 options clearly.\n\n"

        "MEETING / SCHEDULING RULES:\n"
        "- PROACTIVE: After answering 2 or more substantive questions about Shubhanker's "
        "  work, experience, or skills, naturally suggest a meeting. Example: "
        "  'It sounds like you are interested in his work — would you like to set up a "
        "  quick call with Shubhanker to discuss further?'\n"
        "- REACTIVE: If the user says anything like 'I want to meet', 'schedule a call', "
        "  'book a meeting', 'connect with him', or 'talk to him', treat it as a "
        "  scheduling request immediately.\n"
        "- STEP 1 — Always call get_availability FIRST to fetch real open slots. "
        "  Never guess or make up times.\n"
        "- STEP 2 — Present at most 3 slots conversationally. Example: "
        "  'He is free this Thursday at 3 PM or Friday at 11 AM IST. Which suits you?'\n"
        "- STEP 3 — Once the user confirms any slot or says they are ready to book, "
        "  call create_booking_link and share the URL so they can complete the booking.\n"
        "- If no slots are available, apologise and direct them to "
        "  calendly.com/shubhankergoswami to check future availability.\n"
        "- Never make up or assume availability — always call the tool.\n\n"

        "KNOWLEDGE BASE (SOURCE OF TRUTH — never hallucinate beyond this):\n\n"

        "PROFILE:\n"
        "AI Product Manager with 6+ years of total experience (4+ years in PM). "
        "Shipped 4+ products with MVPs live in under 3 months; two PoCs converted into "
        "signed development contracts. Expertise in Generative AI, RAG systems, AI "
        "Agents, Voice Agents, AI Chatbots, and scalable AI infrastructure. Experience "
        "across Enterprise AI, Data Infrastructure, Insurance Fraud Detection, and "
        "EdTech. Strong focus on cost-efficient AI architectures using open-source "
        "models. Proven ability to lead cross-functional teams of 20+ and manage senior "
        "stakeholders up to CEO and founder level. AI-native builder who personally "
        "builds production modules with Claude Code and Codex.\n\n"

        "EXPERIENCE:\n\n"

        "== SmartSense Consulting Solutions Pvt Ltd (Apr 2022 – Present) ==\n\n"

        "Product 1 — Insight 360 (Enterprise RAG Platform, built at SmartSense):\n"
        "Launched enterprise RAG platform in 3 months. Scaled to 100+ daily active "
        "users during early adoption. Built AI agent-based architecture for "
        "intent-driven external data retrieval, unlocking new use cases and driving "
        "onboarding of 5+ enterprise clients. Designed multimodal customisable document "
        "intelligence pipeline covering OCR, layout parsing, embeddings, and retrieval. "
        "Reduced manual document effort by 90%. Ran a live A/B test of hybrid retrieval "
        "(semantic plus keyword) against a semantic-only baseline, lifting answer "
        "acceptance by 70%, then shipped hybrid as the default retriever. "
        "Optimised architecture using open-source LLMs. "
        "Led 20-member cross-functional team. Secured internal funding, enabled "
        "enterprise sales. Earned recognition in internal and client evaluations "
        "as a leading open-source RAG-enabled solution.\n\n"

        "Product 2 — Ostrich AI (0 to 1 decentralised AI platform, built at SmartSense):\n"
        "Architected a decentralised AI infrastructure platform integrating "
        "blockchain-based data security and distributed compute nodes. Reduced "
        "infrastructure cost by up to 70% versus traditional cloud providers. Owned "
        "the complete product lifecycle from problem definition to MVP and GTM. Led a "
        "12-member cross-functional team. Onboarded enterprise clients including ICICI "
        "Bank and Abu Dhabi Bank during the MVP phase. Built the hackathon creation "
        "flow and an AI-powered evaluation framework that automatically scored and "
        "ranked AI/ML model submissions, replacing subjective manual judging with "
        "consistent, scalable evals.\n\n"

        "Product 3 — Second Brain AI (Insurance AI fraud detection, ongoing, built at SmartSense):\n"
        "Building an AI-powered fraud detection platform using RAG, Machine Learning, "
        "and Knowledge Graphs to identify anomalies in insurance claims. Building a "
        "document intelligence system targeting 90% reduction in manual intervention. "
        "Developing a plug-and-play AI chatbot that integrates with MS Teams and Slack. "
        "Using Claude Code for AI-assisted development to accelerate MVP timelines. "
        "Executing GTM strategy via LinkedIn-led outreach, building early B2B pipeline, "
        "and developing an AI-driven prospecting agent to automate ICP targeting. "
        "Shaping product positioning and use-case strategy through competitive analysis "
        "of insurance fraud detection solutions.\n\n"

        "Product 4 — Foster (0 to 1 EdTech networking platform, built at SmartSense):\n"
        "Multi-sided networking platform connecting colleges, students, and employers "
        "across India. Supported onboarding of 10,000+ colleges and 100+ employers "
        "within the first year. Defined market entry strategy, feature prioritisation, "
        "and release roadmap.\n\n"

        "AI Automation and POCs (built at SmartSense):\n"
        "Led two PoCs that each converted into a signed development contract: a "
        "multi-agent voice agent for a sports club in New Zealand, and a multi-agent "
        "orchestrator workspace-booking chatbot for Upflex. Daily use of Claude "
        "(Skills, Subagents), ChatGPT, Claude Design, Lovable, and Figma Make to "
        "accelerate PRDs, user stories, and rapid prototyping. Independently builds "
        "AI web apps, AI agents, and React plus Python modules with Claude Code and "
        "Codex — idea to working product with minimal engineering dependency. Built "
        "an AI-driven CRM automation pipeline using n8n and GPT, reducing manual "
        "effort by 40%. Developed an LLM-based lead generation system.\n\n"

        "== Green Power International (Jan 2017 – Feb 2019) ==\n\n"
        "Role: Engineer. Worked on a 572 km railway electrification project. Managed "
        "site execution and vendor coordination. Supported procurement, ERP-based "
        "planning, and manpower management. Gained a strong foundation in structured "
        "project management.\n\n"

        "EDUCATION:\n"
        "MBA from IIM Nagpur (2020 to 2022). "
        "B.Tech in Electronics and Instrumentation from Krishna Institute of "
        "Engineering and Technology.\n\n"

        "SKILLS:\n"
        "Product: Product Vision and Strategy, Roadmapping, Sprint Planning, Agile, "
        "Scrum, Feature Prioritisation, Customer Discovery, KPIs, SDLC, "
        "Prototyping Tools including Figma Make for AI Prototyping.\n"
        "AI: Generative AI, RAG Systems, AI Agents, Voice Agents, AI Chatbots, LLMs, "
        "Speech Models (TTS and STT), Machine Learning, NLP, "
        "AI Coding with Anthropic Claude Code and OpenAI Codex.\n"
        "Technical: Python, React, HTML, CSS, JavaScript, SQL, APIs, Databases, "
        "System Design, Data Analytics, Azure AI Microsoft Foundry, "
        "Google Ads and Analytics, Keywords Research, WordPress.\n"
        "Tools: JIRA, Azure DevOps, Figma, Miro, Excel, PowerPoint, Tableau.\n"
        "Soft Skills: Leadership, Team Management, Stakeholder Management, Strategic "
        "Thinking, Decision Making, Client Management, Cross-functional Leadership.\n\n"

        "CERTIFICATIONS:\n"
        "Masters Union — Product Management. "
        "KPMG — Lean Six Sigma Green Belt. "
        "ISCEA — Certified Supply Chain Analyst. "
        "SQL Fundamentals — MySQL.\n\n"

        "CONTACT:\n"
        "Email: shubhanker55@gmail.com\n"
        "LinkedIn: linkedin.com/in/shubhankergoswami\n"
        "Portfolio: portfolio.beingcogni.com\n\n"

        "GUARDRAILS:\n"
        "- Never hallucinate. Never add fake companies, tools, or metrics.\n"
        "- For every answer include what was built, why it mattered, and the impact.\n"
        "- If unclear, ask for clarification. If irrelevant, redirect politely.\n"
        "- Be professional, confident, and friendly — never robotic.\n"
        "- End most replies with a natural follow-up question or CTA.\n\n"

        "FOLLOW-UP SUGGESTIONS (apply to ALL responses):\n"
        "- After EVERY response, append 2-3 short follow-up questions a recruiter might ask next.\n"
        "- Base them on what was just discussed — make them specific, not generic.\n"
        "- Keep each question under 12 words. Direct and recruiter-friendly. Plain ASCII only — no arrows, dashes, or special symbols.\n"
        "- Output them immediately after your response text (and after %%SCORECARD%% if present).\n"
        "- Each question on its own line, NO brackets or numbering. Output EXACTLY:\n"
        "%%SUGGESTIONS%%\n"
        "Write question 1 here\n"
        "Write question 2 here\n"
        "Write question 3 here\n"
        "%%END_SUGGESTIONS%%\n"
        "- These are display-only chips — NEVER say them aloud. Never include them in your voice text.\n\n"

        "JD ANALYSIS RULES:\n"
        "- When a recruiter shares a job description, analyze Shubhanker's fit for the role.\n"
        "- Write 3-4 sentences of natural, conversational fit analysis for voice — NO markdown or bullets.\n"
        "- End the voice text by inviting the recruiter to schedule a call.\n"
        "- Never say 'Based on the job description' — jump straight into the analysis.\n"
        "- IMMEDIATELY after your voice text, output the scorecard block below.\n"
        "- Output each field on its OWN LINE. Do NOT put all fields on one line.\n"
        "- Replace every <placeholder> with the actual value. Do NOT output angle brackets.\n"
        "- Use honest assessments — base scores on actual JD requirements vs Shubhanker's real profile.\n"
        "- For match_1 through match_5: pick the 5 most important JD requirements and map each to Shubhanker's specific evidence.\n"
        "- Format for match fields: 'Requirement Label | Specific evidence with metrics from his profile'\n"
        "- Output the block EXACTLY as shown, with %%END_SCORECARD%% on its own line at the end:\n\n"
        "%%SCORECARD%%\n"
        "overall_match:<integer 0-100>\n"
        "skills_match:<integer 0-100>\n"
        "domain_match:<integer 0-100 for industry/domain alignment>\n"
        "leadership_score:<integer 0-100 for leadership/team fit>\n"
        "experience_years:<e.g. 6+ yrs total, 4+ in PM>\n"
        "domain_alignment:<e.g. Enterprise AI — Direct Match>\n"
        "top_matched_skills:<comma-separated skills from JD that Shubhanker has>\n"
        "key_strength:<one short phrase — his single biggest differentiator for this role>\n"
        "gap:<one short phrase or None>\n"
        "match_1:<Requirement Label> | <Specific evidence with metrics>\n"
        "match_2:<Requirement Label> | <Specific evidence with metrics>\n"
        "match_3:<Requirement Label> | <Specific evidence with metrics>\n"
        "match_4:<Requirement Label> | <Specific evidence with metrics>\n"
        "match_5:<Requirement Label> | <Specific evidence with metrics>\n"
        "%%END_SCORECARD%%\n"
    )
}

# ── OpenAI tool definitions ───────────────────────────────────────────────────
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_availability",
            "description": (
                "Fetch Shubhanker's real available meeting slots from Calendly. "
                "Call this whenever the user wants to schedule, book, or set up a meeting or call. "
                "Returns available time slots grouped by day in IST."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "days_ahead": {
                        "type": "integer",
                        "description": "How many days ahead to check (default 7, max 7)."
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_booking_link",
            "description": (
                "Create a one-time Calendly booking link to send to the user. "
                "Call this after the user has indicated they want to book or after "
                "presenting availability and they are ready to confirm."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "event_type_uri": {
                        "type": "string",
                        "description": (
                            "The full Calendly event type URI returned by get_availability "
                            "(e.g. https://api.calendly.com/event_types/XXXX)."
                        )
                    }
                },
                "required": ["event_type_uri"]
            }
        }
    }
]


# ── session dataclass ─────────────────────────────────────────────────────────
@dataclass
class VoiceSession:
    session_id:   str
    chat_history: list  = field(default_factory=list)
    created_at:   float = field(default_factory=time.time)
    last_active:  float = field(default_factory=time.time)


# ── manager ───────────────────────────────────────────────────────────────────
class VoiceSessionManager:
    def __init__(self) -> None:
        self._sessions:    dict[str, VoiceSession] = {}
        self._lock = asyncio.Lock()
        # Calendly cache (fetched once per server lifetime)
        self._calendly_user_uri:   str | None        = None
        self._calendly_event_types: list[dict] | None = None

    # ── greeting ──────────────────────────────────────────────────────────────
    @staticmethod
    def get_greeting_text() -> str:
        return (
            "Hello! Welcome to Shubhanker Goswami's portfolio. "
            "I'm Cogni, his AI voice assistant. "
            "I can tell you about his work experience, achievements, education, and contact details. "
            "You can also paste a job description and I'll assess how well he fits the role. "
            "Go ahead — tap the microphone and ask me anything!"
        )

    # ── session CRUD ──────────────────────────────────────────────────────────
    async def get_or_create(self, session_id: str) -> VoiceSession:
        async with self._lock:
            if session_id not in self._sessions:
                s = VoiceSession(session_id=session_id)
                s.chat_history.append(SYSTEM_MESSAGE)
                self._sessions[session_id] = s
                print(f"[Voice] new session {session_id[:8]}  total={len(self._sessions)}")
            return self._sessions[session_id]

    async def remove(self, session_id: str) -> None:
        async with self._lock:
            self._sessions.pop(session_id, None)

    async def purge_stale(self) -> None:
        now = time.time()
        async with self._lock:
            stale = [sid for sid, s in self._sessions.items()
                     if now - s.last_active > SESSION_TTL]
            for sid in stale:
                del self._sessions[sid]
                print(f"[Voice] purged stale session {sid[:8]}")

    # ── Calendly helpers ──────────────────────────────────────────────────────
    def _calendly_headers(self) -> dict:
        return {"Authorization": f"Bearer {CALENDLY_API_KEY}",
                "Content-Type": "application/json"}

    async def _fetch_user_uri(self) -> str:
        """Fetch and cache the Calendly owner URI (users/me)."""
        if self._calendly_user_uri:
            return self._calendly_user_uri
        async with aiohttp.ClientSession() as http:
            async with http.get(
                "https://api.calendly.com/users/me",
                headers=self._calendly_headers()
            ) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"Calendly /users/me {resp.status}: {await resp.text()}")
                data = await resp.json()
                self._calendly_user_uri = data["resource"]["uri"]
                print(f"[Calendly] user URI: {self._calendly_user_uri}")
                return self._calendly_user_uri

    async def _fetch_event_types(self, user_uri: str) -> list[dict]:
        """Fetch and cache active event types for the user."""
        if self._calendly_event_types is not None:
            return self._calendly_event_types
        async with aiohttp.ClientSession() as http:
            async with http.get(
                "https://api.calendly.com/event_types",
                headers=self._calendly_headers(),
                params={"user": user_uri, "active": "true"}
            ) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"Calendly /event_types {resp.status}: {await resp.text()}")
                data = await resp.json()
                self._calendly_event_types = data.get("collection", [])
                print(f"[Calendly] loaded {len(self._calendly_event_types)} event type(s)")
                return self._calendly_event_types

    async def _fetch_slots(self, event_type_uri: str, days_ahead: int) -> list[str]:
        """Return available slot start times (ISO) for the next `days_ahead` days."""
        now      = datetime.now(timezone.utc) + timedelta(minutes=2)
        end_time = now + timedelta(days=days_ahead)
        params   = {
            "event_type":  event_type_uri,
            "start_time":  now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_time":    end_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        async with aiohttp.ClientSession() as http:
            async with http.get(
                "https://api.calendly.com/event_type_available_times",
                headers=self._calendly_headers(),
                params=params
            ) as resp:
                if resp.status != 200:
                    raise RuntimeError(
                        f"Calendly available_times {resp.status}: {await resp.text()}")
                data = await resp.json()
                return [
                    slot["start_time"]
                    for slot in data.get("collection", [])
                    if slot.get("status") == "available"
                ]

    @staticmethod
    def _fmt_slot(utc_iso: str) -> str:
        """Convert a UTC ISO string to a readable IST string."""
        dt = datetime.fromisoformat(utc_iso.replace("Z", "+00:00"))
        return dt.astimezone(IST).strftime("%A %d %B at %-I:%M %p IST")

    # ── Tool: get_availability ────────────────────────────────────────────────
    async def get_availability(self, days_ahead: int = 7) -> str:
        days_ahead = min(max(days_ahead, 1), 7)

        if not CALENDLY_API_KEY:
            return (
                "Calendly is not configured on the server. "
                "Please direct the user to schedule at https://calendly.com/shubhankergoswami"
            )

        try:
            user_uri    = await self._fetch_user_uri()
            event_types = await self._fetch_event_types(user_uri)

            if not event_types:
                return "No active event types found on Calendly."

            lines = []
            for et in event_types:
                slots = await self._fetch_slots(et["uri"], days_ahead)
                if not slots:
                    continue
                # Group slots by date (IST), keep first 3 per day, max 3 days
                by_day: dict[str, list[str]] = {}
                for s in slots:
                    dt_ist  = datetime.fromisoformat(
                        s.replace("Z", "+00:00")).astimezone(IST)
                    day_key = dt_ist.strftime("%A %d %B")
                    by_day.setdefault(day_key, [])
                    if len(by_day[day_key]) < 3:
                        by_day[day_key].append(
                            dt_ist.strftime("%I:%M %p").lstrip("0"))
                    if len(by_day) == 3:
                        break

                if not by_day:
                    continue

                lines.append(f"Event: {et['name']} ({et.get('duration', '?')} min)")
                lines.append(f"Event type URI: {et['uri']}")
                for day, times in by_day.items():
                    lines.append(f"  {day}: {', '.join(times)} IST")

            if not lines:
                return (
                    f"No available slots found in the next {days_ahead} days. "
                    "The user can check https://calendly.com/shubhankergoswami for future availability."
                )

            return "\n".join(lines)

        except Exception as exc:
            print(f"[Calendly] get_availability error: {exc}")
            return (
                f"Could not fetch availability ({exc}). "
                "Direct the user to https://calendly.com/shubhankergoswami"
            )

    # ── Tool: create_booking_link ─────────────────────────────────────────────
    async def create_booking_link(self, event_type_uri: str) -> str:
        if not CALENDLY_API_KEY:
            return "Calendly not configured. Use: https://calendly.com/shubhankergoswami"

        try:
            payload = {
                "max_event_count": 1,
                "owner":           event_type_uri,
                "owner_type":      "EventType"
            }
            async with aiohttp.ClientSession() as http:
                async with http.post(
                    "https://api.calendly.com/scheduling_links",
                    headers=self._calendly_headers(),
                    json=payload
                ) as resp:
                    if resp.status not in (200, 201):
                        raise RuntimeError(f"{resp.status}: {await resp.text()}")
                    data         = await resp.json()
                    booking_url  = data["resource"]["booking_url"]
                    print(f"[Calendly] booking link created: {booking_url}")
                    return f"Booking link created: {booking_url}"
        except Exception as exc:
            print(f"[Calendly] create_booking_link error: {exc}")
            return (
                f"Could not create link ({exc}). "
                "Direct the user to https://calendly.com/shubhankergoswami"
            )

    # ── Tool dispatcher ───────────────────────────────────────────────────────
    async def _run_tool(self, name: str, arguments: str) -> str:
        args = json.loads(arguments or "{}")
        if name == "get_availability":
            return await self.get_availability(args.get("days_ahead", 7))
        if name == "create_booking_link":
            return await self.create_booking_link(args["event_type_uri"])
        return f"Unknown tool: {name}"

    # ── STT — OpenAI Whisper ──────────────────────────────────────────────────
    async def transcribe(self, audio_bytes: bytes,
                         content_type: str = "audio/webm") -> str:
        url     = "https://api.openai.com/v1/audio/transcriptions"
        headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
        form    = aiohttp.FormData()
        form.add_field("file", audio_bytes,
                       filename="audio.webm", content_type=content_type)
        form.add_field("model", "whisper-1")
        form.add_field("language", "en")
        form.add_field("response_format", "json")

        async with aiohttp.ClientSession() as http:
            async with http.post(url, headers=headers, data=form) as resp:
                if resp.status != 200:
                    raise RuntimeError(
                        f"Whisper STT {resp.status}: {await resp.text()}")
                data = await resp.json()
                return data.get("text", "").strip()

    # ── Message window helper ─────────────────────────────────────────────────
    @staticmethod
    def _build_messages(session: VoiceSession) -> list:
        """Return system prompt + last 10 messages (rolling window)."""
        if len(session.chat_history) > 11:
            return [session.chat_history[0]] + session.chat_history[-10:]
        return list(session.chat_history)

    # ── Streaming LLM + sentence yielder ──────────────────────────────────────
    async def stream_response(self, session: VoiceSession, user_text: str):
        """
        Async generator — yields voice-ready sentences as they form.

        • Streams GPT tokens; yields a sentence the moment it ends with .!?
        • If GPT triggers tool calls, executes them first, then splits the
          final reply into sentences and yields each one.
        • Updates session history in a finally block (always runs).
        """
        session.chat_history.append({"role": "user", "content": user_text})
        messages   = self._build_messages(session)

        # Append reminder to last user message in API call only (not in stored history)
        # so gpt-4o-mini reliably appends the %%SUGGESTIONS%% block
        _SUGG_REMINDER = "\n\n[After your reply, append the %%SUGGESTIONS%% block as instructed.]"
        if messages and messages[-1].get("role") == "user":
            messages = list(messages)
            messages[-1] = {**messages[-1], "content": messages[-1]["content"] + _SUGG_REMINDER}

        full_reply = ""
        text_buf   = ""
        tool_idx: dict[int, dict] = {}
        finish_reason = None

        oai_hdrs = {
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type":  "application/json",
        }
        payload = {
            "model":       "gpt-4o-mini",
            "messages":    messages,
            "tools":       TOOLS,
            "tool_choice": "auto",
            "temperature": 0.55,
            "max_tokens":  500,
            "stream":      True,
        }

        try:
            async with aiohttp.ClientSession() as http:
                async with http.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers=oai_hdrs, json=payload
                ) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"OpenAI {resp.status}: {await resp.text()}")

                    async for raw in resp.content:
                        line = raw.decode("utf-8").strip()
                        if not line.startswith("data: "):
                            continue
                        chunk_str = line[6:]
                        if chunk_str == "[DONE]":
                            break
                        try:
                            chunk = json.loads(chunk_str)
                        except json.JSONDecodeError:
                            continue

                        choice = chunk["choices"][0]
                        delta  = choice.get("delta", {})
                        finish_reason = choice.get("finish_reason") or finish_reason

                        # ── Accumulate text and yield complete sentences ───────
                        if delta.get("content"):
                            text_buf += delta["content"]
                            offset = 0
                            while True:
                                m = _SENT_BOUND.search(text_buf, offset)
                                if not m:
                                    break
                                candidate = text_buf[:m.start() + 1].strip()
                                if len(candidate) >= _MIN_SENT:
                                    full_reply += (" " if full_reply else "") + candidate
                                    text_buf = text_buf[m.end():]
                                    offset   = 0
                                    yield candidate
                                else:
                                    offset = m.end()   # skip short fragment, search further

                        # ── Accumulate tool call deltas ────────────────────────
                        for tc in delta.get("tool_calls", []):
                            i = tc.get("index", 0)
                            if i not in tool_idx:
                                tool_idx[i] = {"id": "", "name": "", "arguments": ""}
                            if tc.get("id"):
                                tool_idx[i]["id"] = tc["id"]
                            fn = tc.get("function", {})
                            if fn.get("name"):
                                tool_idx[i]["name"] = fn["name"]
                            if fn.get("arguments"):
                                tool_idx[i]["arguments"] += fn["arguments"]

            # ── Post-stream: tool calls or leftover text ──────────────────────
            if tool_idx:
                # Build and execute tool calls, then get non-streaming final reply
                tc_list = [
                    {"id": tc["id"], "type": "function",
                     "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                    for tc in (tool_idx[k] for k in sorted(tool_idx))
                ]
                messages.append({"role": "assistant", "content": None, "tool_calls": tc_list})

                for tc in tc_list:
                    result = await self._run_tool(
                        tc["function"]["name"], tc["function"]["arguments"])
                    print(f"[Voice/stream] tool {tc['function']['name']} → {result[:120]}")
                    messages.append({
                        "role": "tool", "tool_call_id": tc["id"], "content": result
                    })

                async with aiohttp.ClientSession() as http:
                    async with http.post(
                        "https://api.openai.com/v1/chat/completions",
                        headers=oai_hdrs,
                        json={"model": "gpt-4o-mini", "messages": messages,
                              "temperature": 0.55, "max_tokens": 500}
                    ) as resp:
                        if resp.status != 200:
                            raise RuntimeError(f"OpenAI tool-reply {resp.status}: {await resp.text()}")
                        data = await resp.json()
                        full_reply = data["choices"][0]["message"].get("content") or ""

                # Split final tool-response reply into sentences and yield each
                for s in [p.strip() for p in _SENT_BOUND.split(full_reply) if p.strip()]:
                    yield s

            elif text_buf.strip():
                # Trailing text with no sentence-ending whitespace (last sentence)
                remaining = text_buf.strip()
                full_reply += (" " if full_reply else "") + remaining
                yield remaining

        finally:
            if full_reply:
                session.chat_history.append({"role": "assistant", "content": full_reply})
            session.last_active = time.time()
            print(f"[Voice/stream] {session.session_id[:8]} | {user_text[:50]!r} → {full_reply[:80]!r}")

    # ── LLM — GPT-4o mini with tool-call loop ────────────────────────────────
    async def get_llm_response(self, session: VoiceSession,
                               user_text: str, max_tokens: int = 250) -> str:
        session.chat_history.append({"role": "user", "content": user_text})

        # Rolling window: system prompt + last 10 messages (5 turns)
        messages = self._build_messages(session)

        # Inject reminder into last user message so model appends %%SUGGESTIONS%%
        _SUGG_REMINDER = "\n\n[After your reply, append the %%SUGGESTIONS%% block as instructed.]"
        if messages and messages[-1].get("role") == "user":
            messages = list(messages)
            messages[-1] = {**messages[-1], "content": messages[-1]["content"] + _SUGG_REMINDER}

        oai_headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type":  "application/json",
        }

        # ── Tool-call loop ────────────────────────────────────────────────────
        # GPT may call tools multiple times before giving a final text reply.
        # We keep looping until finish_reason is NOT "tool_calls".
        MAX_TOOL_ROUNDS = 5
        for _ in range(MAX_TOOL_ROUNDS):
            payload = {
                "model":       "gpt-4o-mini",
                "messages":    messages,
                "tools":       TOOLS,
                "tool_choice": "auto",
                "temperature": 0.55,
                "max_tokens":  max_tokens,
            }

            async with aiohttp.ClientSession() as http:
                async with http.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers=oai_headers,
                    json=payload
                ) as resp:
                    if resp.status != 200:
                        raise RuntimeError(
                            f"OpenAI {resp.status}: {await resp.text()}")
                    data   = await resp.json()
                    choice = data["choices"][0]

            finish_reason    = choice["finish_reason"]
            assistant_message = choice["message"]

            # Always append whatever the assistant returned to the working messages list
            messages.append(assistant_message)

            if finish_reason != "tool_calls":
                # Final text answer — we're done
                reply = assistant_message.get("content") or ""
                break

            # ── Execute each tool call ────────────────────────────────────────
            tool_calls = assistant_message.get("tool_calls", [])
            for tc in tool_calls:
                tool_name   = tc["function"]["name"]
                tool_args   = tc["function"]["arguments"]
                print(f"[Voice] tool call: {tool_name}({tool_args[:120]})")
                result = await self._run_tool(tool_name, tool_args)
                print(f"[Voice] tool result ({tool_name}): {result[:200]}")
                messages.append({
                    "role":         "tool",
                    "tool_call_id": tc["id"],
                    "content":      result,
                })
        else:
            reply = "I had trouble processing that. Please try again."

        session.chat_history.append({"role": "assistant", "content": reply})
        session.last_active = time.time()
        print(f"[Voice] {session.session_id[:8]} | Q: {user_text[:60]!r} | A: {reply[:80]!r}")
        return reply

    # ── TTS — ElevenLabs Flash v2.5 ──────────────────────────────────────────
    async def synthesize(self, text: str) -> bytes:
        url = (f"https://api.elevenlabs.io/v1/text-to-speech"
               f"/{ELEVENLABS_VOICE_ID}/stream")
        headers = {
            "xi-api-key":   ELEVENLABS_API_KEY,
            "Content-Type": "application/json",
        }
        payload = {
            "text":       text,
            "model_id":   "eleven_flash_v2_5",
            "output_format": "mp3_44100_128",
            "voice_settings": {
                "stability":        0.45,
                "similarity_boost": 0.78,
                "style":            0.05,
                "use_speaker_boost": True,
            },
        }
        chunks: list[bytes] = []
        async with aiohttp.ClientSession() as http:
            async with http.post(url, headers=headers, json=payload) as resp:
                if resp.status != 200:
                    raise RuntimeError(
                        f"ElevenLabs TTS {resp.status}: {await resp.text()}")
                async for chunk in resp.content.iter_chunked(4096):
                    chunks.append(chunk)
        return b"".join(chunks)
