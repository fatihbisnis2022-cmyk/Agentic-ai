import hmac
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

import streamlit as st
from pydantic import BaseModel, Field

try:
    import ollama as ollama_lib
except ImportError:
    ollama_lib = None

# ==============================================================================
# 1. KONFIGURASI & PENYIMPANAN MEMORI JSON (ATOMIC WRITE)
# ==============================================================================
DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
MEMORY_FILE = DATA_DIR / "agent_memory.json"


class TaskItem(BaseModel):
    title: str
    status: Literal["pending", "in_progress", "done"] = "pending"
    notes: str = ""
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class AgentMemory(BaseModel):
    preferences: dict[str, str] = Field(
        default_factory=lambda: {
            "bahasa": "Bahasa Indonesia",
            "gaya_jawaban": "Jelas, praktis, dan terstruktur",
        }
    )
    facts: list[str] = Field(default_factory=list)
    tasks: dict[str, TaskItem] = Field(default_factory=dict)


def load_memory() -> AgentMemory:
    if not MEMORY_FILE.exists():
        mem = AgentMemory()
        save_memory_atomic(mem)
        return mem
    try:
        raw = json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
        return AgentMemory(**raw)
    except Exception:
        return AgentMemory()


def save_memory_atomic(memory: AgentMemory) -> None:
    tmp_path = MEMORY_FILE.with_name(f".{MEMORY_FILE.name}.tmp")
    payload = json.dumps(memory.model_dump(), indent=2, ensure_ascii=False)
    with tmp_path.open("w", encoding="utf-8") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, MEMORY_FILE)


# ==============================================================================
# 2. SECRETS / ENVIRONMENT HELPER & LOGIN PIN COOLDOWN BERTINGKAT
# ==============================================================================
def get_secret_or_env(key: str, default: str = "") -> str:
    try:
        if key in st.secrets:
            return str(st.secrets[key])
    except Exception:
        pass
    return os.getenv(key, default)


def hitung_durasi_cooldown(
    lockout_count: int,
    base_seconds: int = 30,
    max_seconds: int = 300,
) -> int:
    durasi = base_seconds * (2**lockout_count)
    return min(durasi, max_seconds)


def require_pin_login(
    title: str = "🔐 Masuk ke Agentic AI",
    max_attempts: int = 3,
    base_cooldown_seconds: int = 30,
    max_cooldown_seconds: int = 300,
) -> None:
    st.session_state.setdefault("authenticated", False)
    st.session_state.setdefault("failed_attempts", 0)
    st.session_state.setdefault("lockout_until", 0.0)
    st.session_state.setdefault("lockout_count", 0)

    if st.session_state.authenticated:
        return

    expected_pin = get_secret_or_env("APP_PIN", "")
    now = time.monotonic()

    st.markdown(f"### {title}")
    st.caption("Masukkan PIN atau password akses untuk membuka aplikasi.")

    if not expected_pin:
        st.error(
            "PIN belum diatur. Tambahkan `APP_PIN` di `st.secrets` atau environment variable `APP_PIN`."
        )
        st.stop()

    if st.session_state.lockout_until > 0 and now >= st.session_state.lockout_until:
        st.session_state.lockout_until = 0.0
        st.session_state.failed_attempts = 0

    if now < st.session_state.lockout_until:
        remaining = max(1, int(st.session_state.lockout_until - now))
        st.warning(
            f"Terlalu banyak percobaan gagal. Silakan tunggu **{remaining} detik** sebelum mencoba lagi."
        )
        st.stop()

    with st.form("pin_login_form", clear_on_submit=True):
        entered_pin = st.text_input("PIN / Password", type="password")
        submitted = st.form_submit_button("Masuk", use_container_width=True)

    if submitted:
        if entered_pin and hmac.compare_digest(entered_pin, expected_pin):
            st.session_state.authenticated = True
            st.session_state.failed_attempts = 0
            st.session_state.lockout_until = 0.0
            st.session_state.lockout_count = 0
            st.rerun()

        st.session_state.failed_attempts += 1
        sisa_percobaan = max_attempts - st.session_state.failed_attempts

        if st.session_state.failed_attempts >= max_attempts:
            durasi = hitung_durasi_cooldown(
                lockout_count=st.session_state.lockout_count,
                base_seconds=base_cooldown_seconds,
                max_seconds=max_cooldown_seconds,
            )
            st.session_state.lockout_until = time.monotonic() + durasi
            st.session_state.lockout_count += 1
            st.error(
                f"Percobaan salah mencapai batas ({max_attempts}x). Akses dijeda selama **{durasi} detik**."
            )
            st.stop()
        else:
            st.error(
                f"PIN atau password salah. Sisa percobaan sebelum jeda: **{sisa_percobaan}x**."
            )

    st.stop()


def render_logout_button() -> None:
    if st.session_state.get("authenticated"):
        if st.sidebar.button("🚪 Keluar (Logout)", use_container_width=True):
            st.session_state.authenticated = False
            st.session_state.failed_attempts = 0
            st.session_state.lockout_until = 0.0
            st.session_state.lockout_count = 0
            st.rerun()


# ==============================================================================
# 3. TOOLS UNTUK AGEN MULTI-LANGKAH
# ==============================================================================
def simpan_preferensi(kunci: str, nilai: str) -> str:
    """Simpan atau perbarui preferensi pengguna ke memori JSON."""
    mem = load_memory()
    mem.preferences[kunci.strip()] = nilai.strip()
    save_memory_atomic(mem)
    return f"Preferensi '{kunci}' berhasil disimpan dengan nilai '{nilai}'."


def tambah_fakta_memori(fakta: str) -> str:
    """Simpan fakta, keputusan, atau catatan penting ke memori jangka panjang."""
    mem = load_memory()
    bersih = fakta.strip()
    if bersih and bersih not in mem.facts:
        mem.facts.append(bersih)
        save_memory_atomic(mem)
        return f"Fakta disimpan ke memori: {bersih}"
    return "Fakta sudah ada di memori atau kosong."


def perbarui_tugas(
    judul: str,
    status: Literal["pending", "in_progress", "done"],
    catatan: str = "",
) -> str:
    """Buat atau perbarui status tugas pada memori JSON."""
    mem = load_memory()
    key = judul.strip()
    mem.tasks[key] = TaskItem(title=key, status=status, notes=catatan.strip())
    save_memory_atomic(mem)
    return f"Tugas '{key}' diperbarui menjadi status '{status}'."


def baca_ringkasan_memori() -> str:
    """Baca preferensi, fakta, dan daftar tugas yang tersimpan di memori JSON."""
    mem = load_memory()
    return json.dumps(mem.model_dump(), indent=2, ensure_ascii=False)


# ==============================================================================
# 4. ADAPTER MESIN AI (GEMINI & OLLAMA PYTHON LIBRARY)
# ==============================================================================
class EnrichedPromptResult(BaseModel):
    enriched_prompt: str
    context_used: list[str]


def _extract_json_block(text: str) -> str:
    bersih = text.strip()
    if bersih.startswith("```"):
        lines = bersih.splitlines()
        if len(lines) >= 3:
            bersih = "\n".join(lines[1:-1]).strip()
    return bersih


def enrich_prompt(
    provider: Literal["Gemini", "Ollama (Library Python)"],
    model_name: str,
    raw_prompt: str,
    memory: AgentMemory,
    gemini_api_key: str = "",
    ollama_host: str = "http://localhost:11434",
) -> EnrichedPromptResult:
    prefs_str = (
        "\n".join(f"- {k}: {v}" for k, v in memory.preferences.items()) or "- Tidak ada"
    )
    facts_str = "\n".join(f"- {f}" for f in memory.facts[-15:]) or "- Tidak ada"
    tasks_str = (
        "\n".join(
            f"- [{t.status}] {t.title}" + (f" ({t.notes})" if t.notes else "")
            for t in memory.tasks.values()
        )
        or "- Tidak ada tugas aktif"
    )

    meta_prompt = f"""
Kamu adalah Prompt Refiner. Tugasmu menyempurnakan input pengguna menjadi prompt perintah langsung yang jelas, spesifik, dan siap dijalankan oleh agen AI.

Aturan:
1. Pertahankan maksud asli pengguna; jangan menambah fitur atau asumsi teknis baru yang tidak diminta maupun tidak ada di memori.
2. Masukkan preferensi, fakta, atau tugas dari memori hanya jika relevan dengan permintaan pengguna.
3. Susun enriched_prompt sebagai perintah langsung yang tegas, terstruktur, dan mudah dieksekusi.

Preferensi Pengguna:
{prefs_str}

Fakta Memori Tersimpan:
{facts_str}

Daftar Tugas Saat Ini:
{tasks_str}

Input Mentah Pengguna:
"{raw_prompt}"
""".strip()

    if provider == "Gemini":
        api_key_input = st.text_input(
            "Gemini API Key",
            value=default_api_key,
            type="password",
            help="Otomatis terisi jika GEMINI_API_KEY ada di st.secrets atau environment variable.",
        )
        ollama_host = "http://localhost:11434"
        model_name = st.text_input(
            "Model Gemini",
            value=get_secret_or_env("GEMINI_MODEL", "gemini-3.8-flash"),
            help="Sesuaikan dengan model aktif dari hasil client.models.list(), mis. gemini-3.8-flash.",
        )
              (resp.text)

    if ollama_lib is None:
        raise RuntimeError(
            "Library 'ollama' belum terinstal. Tambahkan 'ollama' ke requirements.txt."
        )

    client_ollama = ollama_lib.Client(
        host=ollama_host.strip() or "http://localhost:11434"
    )
    schema_hint = json.dumps(EnrichedPromptResult.model_json_schema(), ensure_ascii=False)
    prompt_with_schema = (
        f"{meta_prompt}\n\n"
        "Balas HANYA dalam format JSON valid yang sesuai dengan skema berikut:\n"
        f"{schema_hint}"
    )
    resp = client_ollama.chat(
        model=model_name,
        messages=[{"role": "user", "content": prompt_with_schema}],
        options={"temperature": 0.2},
    )
    raw_content = _extract_json_block(resp.message.content or "{}")
    return EnrichedPromptResult.model_validate_json(raw_content)


def run_agent_turn(
    provider: Literal["Gemini", "Ollama (Library Python)"],
    model_name: str,
    enriched_prompt: str,
    chat_history: list[dict[str, str]],
    gemini_api_key: str = "",
    ollama_host: str = "http://localhost:11434",
    max_steps: int = 5,
) -> tuple[str, list[str]]:
    tools_used: list[str] = []

    def wrap_tool(fn: Callable[..., str], label: str) -> Callable[..., str]:
        def _inner(**kwargs: Any) -> str:
            args_repr = ", ".join(f"{k}={repr(v)}" for k, v in kwargs.items())
            tools_used.append(f"{label}({args_repr})")
            return fn(**kwargs)

        _inner.__name__ = fn.__name__
        _inner.__doc__ = fn.__doc__
        return _inner

    tracked_tools = [
        wrap_tool(simpan_preferensi, "simpan_preferensi"),
        wrap_tool(tambah_fakta_memori, "tambah_fakta_memori"),
        wrap_tool(perbarui_tugas, "perbarui_tugas"),
        wrap_tool(baca_ringkasan_memori, "baca_ringkasan_memori"),
    ]
    tool_map = {fn.__name__: fn for fn in tracked_tools}

    system_instruction = (
        "Kamu adalah asisten Agentic AI yang membantu menyelesaikan tugas secara terstruktur. "
        "Gunakan tool memori bila pengguna menyampaikan preferensi baru, fakta penting proyek, "
        "atau progres tugas, maupun saat perlu membaca memori/tugas yang tersimpan. "
        "Setelah selesai, berikan jawaban akhir yang jelas dan ringkas."
    )

    if provider == "Gemini":
        from google import genai
        from google.genai import types

        client = (
            genai.Client(api_key=gemini_api_key.strip())
            if gemini_api_key.strip()
            else genai.Client()
        )

        contents: list[types.Content] = []
        for msg in chat_history[-10:]:
            role = "user" if msg["role"] == "user" else "model"
            contents.append(
                types.Content(
                    role=role,
                    parts=[types.Part.from_text(text=msg["content"])],
                )
            )
        contents.append(
            types.Content(
                role="user",
                parts=[types.Part.from_text(text=enriched_prompt)],
            )
        )

        response = client.models.generate_content(
            model=model_name,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                tools=tracked_tools,
                temperature=0.3,
            ),
        )
        return response.text or "(Tidak ada respons teks.)", tools_used

    if ollama_lib is None:
        raise RuntimeError(
            "Library 'ollama' belum terinstal. Tambahkan 'ollama' ke requirements.txt."
        )

    client_ollama = ollama_lib.Client(
        host=ollama_host.strip() or "http://localhost:11434"
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_instruction},
    ]
    for msg in chat_history[-10:]:
        role = "user" if msg["role"] == "user" else "assistant"
        messages.append({"role": role, "content": msg["content"]})
    messages.append({"role": "user", "content": enriched_prompt})

    for _ in range(max_steps):
        resp = client_ollama.chat(
            model=model_name,
            messages=messages,
            tools=tracked_tools,
            options={"temperature": 0.3},
        )
        msg_obj = resp.message
        messages.append(msg_obj)

        tool_calls = getattr(msg_obj, "tool_calls", None) or []
        if not tool_calls:
            return msg_obj.content or "(Tidak ada respons teks.)", tools_used

        for call in tool_calls:
            fn_name = call.function.name
            fn_args = call.function.arguments or {}
            if isinstance(fn_args, str):
                fn_args = json.loads(fn_args)

            if fn_name in tool_map:
                try:
                    result = tool_map[fn_name](**fn_args)
                except Exception as exc:
                    result = f"Error menjalankan tool '{fn_name}': {exc}"
            else:
                result = f"Tool '{fn_name}' tidak ditemukan."

            messages.append({"role": "tool", "content": str(result)})

    return "Batas langkah agen tercapai sebelum jawaban akhir dihasilkan.", tools_used


# ==============================================================================
# 5. ANTARMUKA STREAMLIT
# ==============================================================================
st.set_page_config(
    page_title="Agentic AI + JSON Memory",
    page_icon="🤖",
    layout="wide",
)

require_pin_login()

if "messages" not in st.session_state:
    st.session_state.messages = []

default_api_key = get_secret_or_env("GEMINI_API_KEY", "")

with st.sidebar:
    render_logout_button()
    st.header("⚙️ Pengaturan Agen")

    provider = st.selectbox(
        "Pilih Mesin AI",
        options=["Gemini", "Ollama (Library Python)"],
        index=0,
    )

    if provider == "Gemini":
        api_key_input = st.text_input(
            "Gemini API Key",
            value=default_api_key,
            type="password",
            help="Otomatis terisi jika GEMINI_API_KEY ada di st.secrets atau environment variable.",
        )
        ollama_host = "http://localhost:11434"
        model_name = st.selectbox(
            "Model Gemini",
            options=["gemini-2.5-flash", "gemini-2.5-pro"],
            index=0,
        )
    else:
        api_key_input = ""
        ollama_host = st.text_input(
            "Ollama Host URL",
            value=get_secret_or_env("OLLAMA_HOST", "http://localhost:11434"),
            help="URL server Ollama yang aktif (lokal atau server/tunnel yang dapat dijangkau).",
        )
        model_name = st.text_input(
            "Nama Model Ollama",
            value="llama3.2",
            help="Pastikan model sudah tersedia di server Ollama (mis. llama3.2, qwen2.5).",
        )

    use_enrichment = st.toggle(
        "Aktifkan Prompt Enrichment",
        value=True,
        help="Sempurnakan prompt user dengan konteks memori sebelum dikirim ke agen.",
    )

    st.divider()
    st.subheader("🧠 Memori JSON")
    current_mem = load_memory()
    st.json(current_mem.model_dump())

    col_reset_chat, col_reset_mem = st.columns(2)
    with col_reset_chat:
        if st.button("Hapus Chat", use_container_width=True):
            st.session_state.messages = []
            st.rerun()
    with col_reset_mem:
        if st.button("Reset Memori", use_container_width=True):
            save_memory_atomic(AgentMemory())
            st.rerun()

st.title("🤖 Agentic AI dengan Memori JSON")
st.caption(
    "Ketik perintah bebas — sistem menyempurnakan prompt dengan memori JSON, lalu menjalankan agen multi-langkah dengan tool."
)

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if msg.get("enriched_prompt"):
            with st.expander("✨ Enriched Prompt & Detail Eksekusi"):
                st.code(msg["enriched_prompt"], language="text")
                if msg.get("context_used"):
                    st.write("**Konteks dipakai:**", ", ".join(msg["context_used"]))
                if msg.get("tools_used"):
                    st.write("**Tool dipanggil:**")
                    for t in msg["tools_used"]:
                        st.markdown(f"- `{t}`")

user_input = st.chat_input(
    "Contoh: Ingat saya pakai Python & FastAPI, lalu buatkan tugas 'setup auth JWT'"
)

if user_input:
    effective_key = api_key_input.strip() or get_secret_or_env("GEMINI_API_KEY", "").strip()
    if provider == "Gemini" and not effective_key:
        st.error(
            "Masukkan `GEMINI_API_KEY` di sidebar atau atur di `st.secrets` / environment variable `GEMINI_API_KEY`."
        )
        st.stop()

    st.session_state.messages.append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.markdown(user_input)

    with st.chat_message("assistant"):
        with st.spinner("Menyempurnakan prompt & menjalankan agen..."):
            try:
                memory_snapshot = load_memory()

                if use_enrichment:
                    enriched_res = enrich_prompt(
                        provider=provider,
                        model_name=model_name,
                        raw_prompt=user_input,
                        memory=memory_snapshot,
                        gemini_api_key=effective_key,
                        ollama_host=ollama_host,
                    )
                    final_prompt = enriched_res.enriched_prompt
                    context_used = enriched_res.context_used
                else:
                    final_prompt = user_input
                    context_used = []

                assistant_reply, tools_used = run_agent_turn(
                    provider=provider,
                    model_name=model_name,
                    enriched_prompt=final_prompt,
                    chat_history=st.session_state.messages[:-1],
                    gemini_api_key=effective_key,
                    ollama_host=ollama_host,
                )

                st.markdown(assistant_reply)
                if use_enrichment or tools_used:
                    with st.expander("✨ Enriched Prompt & Detail Eksekusi"):
                        st.code(final_prompt, language="text")
                        if context_used:
                            st.write("**Konteks dipakai:**", ", ".join(context_used))
                        if tools_used:
                            st.write("**Tool dipanggil:**")
                            for t in tools_used:
                                st.markdown(f"- `{t}`")

                st.session_state.messages.append(
                    {
                        "role": "assistant",
                        "content": assistant_reply,
                        "enriched_prompt": final_prompt if use_enrichment else "",
                        "context_used": context_used,
                        "tools_used": tools_used,
                    }
                )
            except Exception as exc:
                err_msg = f"Terjadi kesalahan saat menjalankan agen: `{exc}`"
                st.error(err_msg)
                st.session_state.messages.append(
                    {"role": "assistant", "content": err_msg}
                )
