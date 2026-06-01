import json
import types
from unittest.mock import MagicMock, patch
import pytest

from scratch import ScratchPad

def test_send_to_ollama_worker_logic():
    """
    Integration test validating that the _send_to_ollama streaming logic
    correctly triggers threads and emits text chunks via signals.
    """
    app_mock = MagicMock()
    app_mock._chat_pages = {0: []}
    app_mock._panes = [MagicMock(page_index=0)]

    app_mock._active_ollama_profile().get.return_value = {"options": {"num_ctx": 2048}}
    app_mock.config.get().get().rstrip.return_value = "http://localhost:11434"

    # _send_to_ollama calls self._stream_ollama_chat, but self is a MagicMock so
    # that call would be silently absorbed.  Bind the real method so the thread
    # is actually created and can be captured below.
    app_mock._stream_ollama_chat = types.MethodType(
        ScratchPad._stream_ollama_chat, app_mock
    )

    # --- THE FIXED PATCH TARGETS ---
    # Mock urlopen so the thread worker doesn't hit the network, then mock the
    # stream parsers so we control what chunks the worker emits.
    with patch("scratch.urllib.request.urlopen"), \
         patch("scratch.ollama_stream_chunks") as mock_stream, \
         patch("scratch.ollama_chat_stream_chunks") as mock_chat_stream, \
         patch("scratch.threading.Thread") as mock_thread_class:
        
        # Simulate text chunks coming out of your custom parser functions
        mock_stream.return_value = ["AI", " Response"]
        mock_chat_stream.return_value = ["AI", " Response"]

        captured_stream_worker = []

        def thread_constructor_hook(*args, **kwargs):
            target_fn = kwargs.get("target")
            if target_fn:
                captured_stream_worker.append(target_fn)
            return MagicMock()

        mock_thread_class.side_effect = thread_constructor_hook

        # Execute your core application wrapper method
        ScratchPad._send_to_ollama(app_mock, prompt="Hello Ollama", chat_page_idx=0)

        # Force the captured inner _stream thread function to execute synchronously
        if captured_stream_worker:
            captured_stream_worker[0]()

    # Assert: Verify that your thread loop actually attempted to run a parser
    assert mock_stream.called or mock_chat_stream.called

    # Assert: Verify your safe Qt signals were emitted to feed your UI engine logs
    assert app_mock._ollama_chunk.emit.called
