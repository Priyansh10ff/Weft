"""Streamlit frontend for the Weft multimodal RAG API."""

from pathlib import Path
from typing import Any

import requests
import streamlit as st


BACKEND_URL = "http://localhost:8000"

# Complete routing table covering all 4 modalities and 11 file extensions.
UPLOAD_ENDPOINTS: dict[str, str] = {
    # Video
    ".mp4": "/upload/video",
    ".mov": "/upload/video",
    ".avi": "/upload/video",
    # Audio
    ".mp3": "/upload/audio",
    ".wav": "/upload/audio",
    ".m4a": "/upload/audio",
    # PDF
    ".pdf": "/upload/pdf",
    # Image
    ".png": "/upload/image",
    ".jpg": "/upload/image",
    ".jpeg": "/upload/image",
}


st.set_page_config(
    page_title="Weft: Multimodal RAG Pipeline",
    page_icon="🧠",
    layout="wide",
)

st.title("Weft: Multimodal RAG Pipeline")
st.caption("Search across transcripts, visual evidence, and document context.")


def _error_detail(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text or f"HTTP {response.status_code}"
    return str(payload.get("detail", payload)) if isinstance(payload, dict) else str(payload)


def _upload_source(uploaded_file: Any) -> None:
    extension = Path(uploaded_file.name).suffix.lower()
    endpoint = UPLOAD_ENDPOINTS.get(extension)
    if endpoint is None:
        st.error(
            f"Unsupported file type: `{extension}`. "
            "Supported: mp4, mov, avi, mp3, wav, m4a, pdf, png, jpg, jpeg."
        )
        return

    files = {
        "file": (
            uploaded_file.name,
            uploaded_file.getvalue(),
            uploaded_file.type or "application/octet-stream",
        )
    }
    try:
        with st.spinner("Processing and indexing your knowledge source…"):
            response = requests.post(
                f"{BACKEND_URL}{endpoint}",
                files=files,
                timeout=600,
            )
        if not response.ok:
            st.error(f"Upload failed: {_error_detail(response)}")
            return
        result = response.json()
        # Normalise across VideoUploadResponse / KnowledgeUploadResponse shapes.
        indexed_count = result.get("indexed_count") or result.get("processed_nodes", 0)
        st.success(f"Indexed {indexed_count} KnowledgeNode(s).")
        st.toast("Knowledge source indexed successfully.")
    except requests.RequestException as exc:
        st.error(f"Could not reach the FastAPI backend: {exc}")
    except ValueError:
        st.error("The backend returned an invalid upload response.")


def _display_result(result: dict[str, Any] | None, *, multimodal: bool) -> None:
    if not result:
        st.info("No result found.")
        return

    if multimodal:
        # --- Spoken transcript ---
        st.markdown("**Spoken Transcript**")
        # Show transcript if present, otherwise fall back to content.
        # Only show the "unavailable" message if BOTH fields are missing.
        transcript = result.get("transcript") or result.get("content")
        if transcript:
            st.write(transcript)
        else:
            st.caption("No transcript available.")

        # --- Visual summary ---
        visual_summary = result.get("visual_summary")
        if visual_summary:
            st.markdown("**Visual Summary**")
            st.write(visual_summary)

        # --- Timestamp ---
        timestamp = result.get("timestamp")
        if timestamp:
            st.markdown("**Timestamp / Location**")
            # Render as plain text — never show raw JSON brackets.
            st.write(str(timestamp))

        # --- Frame image ---
        frame_path = result.get("frame_path")
        # Guard: only render if frame_path is a non-empty string that is not "0".
        if frame_path and isinstance(frame_path, str) and frame_path.strip() not in ("", "0"):
            frame_url = f"{BACKEND_URL}{frame_path}"
            try:
                st.image(frame_url, caption="Extracted visual evidence")
            except Exception:
                st.caption(f"Frame preview unavailable: {frame_path}")
    else:
        # --- Text-only baseline ---
        st.markdown("**Raw Spoken Transcript**")
        transcript = result.get("transcript") or result.get("content")
        if transcript:
            st.write(transcript)
        else:
            st.caption("No transcript available.")
        if not result.get("visual_summary"):
            st.warning(":material/visibility_off: Visual evidence missed by text-only search!")


with st.sidebar:
    st.header("1. Upload Knowledge Source")
    uploaded_file = st.file_uploader(
        "Choose a source file",
        # All 11 supported extensions across all 4 modalities.
        type=["mp4", "mov", "avi", "mp3", "wav", "m4a", "pdf", "png", "jpg", "jpeg"],
        help=(
            "Upload a video (mp4, mov, avi), audio (mp3, wav, m4a), "
            "PDF, or image (png, jpg, jpeg) knowledge source."
        ),
    )
    if uploaded_file is not None:
        upload_key = f"{uploaded_file.name}:{uploaded_file.size}"
        if st.session_state.get("uploaded_key") != upload_key:
            st.session_state["uploaded_key"] = upload_key
            _upload_source(uploaded_file)

st.header("2. Query & Baseline Comparison")
query = st.text_input(
    "Search question",
    placeholder="What database architecture was discussed?",
)
if st.button("Search & Compare", type="primary"):
    if not query.strip():
        st.warning("Enter a search question first.")
    else:
        try:
            with st.spinner("Searching multimodal and text-only indexes…"):
                response = requests.post(
                    f"{BACKEND_URL}/query/compare",
                    json={"query": query, "limit": 5},
                    timeout=120,
                )
            if not response.ok:
                st.error(f"Search failed: {_error_detail(response)}")
            else:
                comparison = response.json()
                left_column, right_column = st.columns(2)
                with left_column:
                    st.subheader(":material/auto_awesome: Multimodal RAG Result")
                    _display_result(comparison.get("multimodal_result"), multimodal=True)
                with right_column:
                    st.subheader(":material/warning: Text-Only Baseline Result")
                    _display_result(
                        comparison.get("text_only_baseline_result"), multimodal=False
                    )
        except requests.RequestException as exc:
            st.error(f"Could not reach the FastAPI backend: {exc}")
        except ValueError:
            st.error("The backend returned an invalid comparison response.")
