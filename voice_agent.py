"""
voice_agent.py – Session-aware voice pipeline
  STT  : OpenAI Whisper (whisper-1)
  LLM  : GPT-4o mini  (with Calendly tool calling)
  TTS  : ElevenLabs Flash v2.5
  TOOL : Calendly – get availability + create booking link
"""

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import aiohttp

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
        "AI Product Manager with 4+ years of experience. Expertise in Generative AI, "
        "RAG systems, AI Agents, and scalable AI infrastructure. Experience across "
        "Enterprise AI, Data Infrastructure, Insurance Fraud Detection, and EdTech. "
        "Strong focus on cost-efficient AI architectures using open-source models. "
        "Proven ability to launch MVPs in under 3 months, drive early enterprise "
        "adoption, and lead cross-functional teams of up to 20 members. "
        "Hands-on AI development using tools like Claude Code.\n\n"

        "EXPERIENCE:\n\n"

        "Insight 360 (Enterprise RAG Platform — Client: BASF):\n"
        "Launched enterprise RAG platform in 3 months. Scaled to 100+ daily active "
        "users during early adoption. Built AI agent-based architecture for "
        "intent-driven external data retrieval. Designed multimodal document "
        "intelligence pipeline covering OCR, layout parsing, embeddings, and "
        "retrieval. Enabled multi-format knowledge access across enterprise documents. "
        "Reduced manual document effort by 90%. Improved retrieval accuracy by 70% "
        "using hybrid search combining semantic and keyword-based retrieval. "
        "Optimised architecture using open-source LLMs to reduce dependency on paid "
        "APIs and improve cost efficiency. Led 20-member cross-functional team across "
        "engineering, design, and QA. Crafted product narrative and investment pitch, "
        "secured internal funding, and enabled enterprise sales and onboarding of 5+ "
        "clients. Recognised internally and by clients as a leading RAG-enabled "
        "solution.\n\n"

        "Ostrich AI (0 to 1 Product):\n"
        "Architected a decentralised AI infrastructure platform integrating "
        "blockchain-based data security and distributed compute nodes to enable "
        "secure AI and ML model deployment. Reduced infrastructure cost by up to 70% "
        "versus traditional cloud providers. Owned the complete product lifecycle from "
        "problem definition and product vision through MVP and go-to-market. "
        "Translated ambiguous enterprise problems into scalable AI solutions. "
        "Led a 12-member cross-functional team. Onboarded enterprise clients including "
        "ICICI Bank and Abu Dhabi Bank during the MVP phase. Conducted user research "
        "and market analysis to identify unmet needs and product-market fit. Designed "
        "user journeys, wireframes, sprint planning, and prioritisation frameworks.\n\n"

        "Second Brain AI (Insurance AI — Ongoing 0 to 1 Product):\n"
        "Building an AI-powered fraud detection platform using RAG, Machine Learning, "
        "and Knowledge Graphs to detect anomalies in insurance claims. Building a "
        "document intelligence system for claims processing targeting 90% reduction "
        "in manual intervention. Developing a plug-and-play AI chatbot that handles "
        "employee policy and claims queries and integrates with MS Teams and Slack. "
        "Using AI-assisted development with Claude Code to build core modules "
        "independently, accelerate MVP timelines, and reduce engineering dependency. "
        "Executing GTM strategy via LinkedIn outreach and building an AI-driven "
        "prospecting agent that automates ICP targeting and engagement.\n\n"

        "Foster (0 to 1 Platform):\n"
        "Multi-sided networking platform connecting colleges, students, and employers "
        "across India. Supported onboarding of 10,000+ colleges and 100+ employers "
        "within the first year. Drove product direction and execution. Defined market "
        "entry strategy, feature prioritisation, and release roadmap. Leveraged user "
        "insights, feedback, and performance data to improve engagement. Collaborated "
        "on pricing strategy and long-term roadmap to influence revenue and retention.\n\n"

        "AI Automation and POCs:\n"
        "Built an AI-driven CRM automation pipeline using n8n and GPT that reduced "
        "manual effort by 40% and improved lead management efficiency. Developed an "
        "LLM-based lead generation system that identifies and qualifies prospects. "
        "Built an AI voice assistant POC that handles inbound calls and enabled "
        "successful client onboarding.\n\n"

        "Early Career — Engineering (Green Power International):\n"
        "Worked on a 572 km railway electrification project. Managed site execution "
        "and vendor coordination. Supported procurement, ERP-based planning, and "
        "manpower management. Worked with senior leadership on planning and billing. "
        "Gained a strong foundation in structured project management.\n\n"

        "EDUCATION:\n"
        "MBA from IIM Nagpur (2020 to 2022). "
        "B.Tech in Electronics and Instrumentation from Krishna Institute of "
        "Engineering and Technology.\n\n"

        "SKILLS:\n"
        "Product: Product Vision and Strategy, Roadmapping, Sprint Planning, Agile, "
        "Scrum, Feature Prioritisation, Customer Discovery, KPIs, SDLC.\n"
        "AI: Generative AI, RAG Systems, AI Agents, LLMs, NLP, TTS, STT.\n"
        "Technical: Python, React, HTML, CSS, JavaScript, SQL, APIs, Databases, "
        "System Design, Data Analytics.\n"
        "Tools: JIRA, Azure DevOps, Figma, Miro, Excel, PowerPoint, Tableau.\n"
        "Soft Skills: Leadership, Team Management, Stakeholder Management, Strategic "
        "Thinking, Decision Making, Client Management.\n\n"

        "CONTACT:\n"
        "Email: shubhanker55@gmail.com\n"
        "LinkedIn: linkedin.com/in/shubhankergoswami\n"
        "Portfolio: portfolio.beingcogni.com\n\n"

        "GUARDRAILS:\n"
        "- Never hallucinate. Never add fake companies, tools, or metrics.\n"
        "- For every answer include what was built, why it mattered, and the impact.\n"
        "- If unclear, ask for clarification. If irrelevant, redirect politely.\n"
        "- Be professional, confident, and friendly — never robotic.\n"
        "- End most replies with a natural follow-up question or CTA."
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

    # ── LLM — GPT-4o mini with tool-call loop ────────────────────────────────
    async def get_llm_response(self, session: VoiceSession,
                               user_text: str) -> str:
        session.chat_history.append({"role": "user", "content": user_text})

        # Rolling window: system prompt + last 10 messages (5 turns)
        messages = (
            [session.chat_history[0]] + session.chat_history[-10:]
            if len(session.chat_history) > 11
            else list(session.chat_history)
        )

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
                "max_tokens":  250,
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
