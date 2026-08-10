from scratch_core import ollama_chat_stream_chunks, ollama_stream_chunks


def test_ollama_stream_chunks_success():
    """
    Verifies that standard, well-formed JSON chunks are decoded
    and their values are successfully yielded.
    """
    # 1. Arrange: Simulate an array of binary bytes coming from urllib
    mock_network_stream = [
        b'{"response": "Hello", "done": false}',
        b'{"response": " world", "done": false}',
        b'{"response": "!", "done": true}'
    ]

    # 2. Act: Pass the stream iterator to your function and convert to a list
    result = list(ollama_stream_chunks(mock_network_stream))

    # 3. Assert: Verify the generator decoded and extracted the string fragments
    assert result == ["Hello", " world", "!"]


def test_ollama_stream_chunks_ignores_empty_and_broken_lines():
    """
    Verifies that the function safely skips whitespace lines
    and malformed JSON strings instead of crashing.
    """
    mock_dirty_stream = [
        b' ',                                     # Empty string check (line 5)
        b'{"response": "Valid text"}',           # Good chunk
        b'{"response": "", "done": false}',      # Empty text block check (line 11)
        b'INVALID GLOBAL JSON OBJECT TEXT HERE',  # Corrupted data check (line 8)
        b'{"response": " Done.", "done": true}'  # Final chunk
    ]

    result = list(ollama_stream_chunks(mock_dirty_stream))
    assert result == ["Valid text", " Done."]


def test_ollama_stream_chunks_breaks_on_done():
    """
    Verifies that the function respects the "done" flag and stops
    iterating immediately, even if more data follows it in the stream.
    """
    mock_early_done_stream = [
        b'{"response": "First", "done": false}',
        b'{"response": "Second", "done": true}',
        b'{"response": "Third hidden chunk", "done": false}'  # Should be ignored
    ]

    result = list(ollama_stream_chunks(mock_early_done_stream))
    assert result == ["First", "Second"]


def test_ollama_stream_chat_chunks_success():
    """
    Verifies that nested chat JSON chunks are successfully
    decoded and the message text content is yielded.
    """
    mock_chat_stream = [
        b'{"message": {"role": "assistant", "content": "How "}, "done": false}',
        b'{"message": {"role": "assistant", "content": "can "}, "done": false}',
        b'{"message": {"role": "assistant", "content": "I help?"}, "done": true}'
    ]

    # Act: Target the correct chat-specific streaming parser
    result = list(ollama_chat_stream_chunks(mock_chat_stream))

    # Assert
    assert result == ["How ", "can ", "I help?"]


def test_ollama_stream_chat_chunks_handles_missing_keys_and_bad_data():
    """
    Verifies that the chat parser doesn't crash if a chunk
    is missing the 'message' dictionary or contains invalid data.
    """
    mock_corrupted_chat = [
        b'{"message": {"content": "Valid"}, "done": false}',
        b'{"unexpected_format": "garbage"}',
        b'{"message": null, "done": false}',
        b'{"message": {"content": " End"}, "done": true}'
    ]

    # Act: Target the correct chat-specific streaming parser
    result = list(ollama_chat_stream_chunks(mock_corrupted_chat))

    # Assert
    assert result == ["Valid", " End"]
