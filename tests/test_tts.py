"""Jarvis's voice: markdown stripping, and the chunking that lets him start speaking
before the whole reply has been synthesised.

The chunking is the part worth testing hard. Its entire job is to put the seams where a
person would pause -- a break after "Mr." or inside "$63.41" is instantly audible as a
machine reading badly, and costs more than the latency it saved. None of these tests
load Piper: splitting text is pure logic and must not need a 60MB voice model to verify.
"""
import pytest

from assistant.core import tts
from assistant.core.tts import Speaker, split_for_speech


class TestClean:
    """The web UI strips markdown in JS; a kiosk never runs that code, so the server has
    to do it too or '**Chime Checking**' is read out as 'asterisk asterisk'."""

    def test_bold_and_italic_markers_are_not_spoken(self):
        assert Speaker.clean("**Chime Checking** and *this*") == "Chime Checking and this"

    def test_code_fences_are_dropped_entirely(self):
        assert "print" not in Speaker.clean("Here:\n```python\nprint('hi')\n```\ndone")

    def test_a_link_keeps_its_text_and_loses_its_url(self):
        assert Speaker.clean("see [the board](https://example.com/x)") == "see the board"

    def test_list_and_heading_markers_go(self):
        assert Speaker.clean("## Title\n- one\n- two") == "Title\none\ntwo"

    def test_empty_input_is_empty_not_an_error(self):
        assert Speaker.clean("") == ""
        assert Speaker.clean(None) == ""


class TestSplitForSpeech:
    def test_nothing_to_say_yields_no_chunks(self):
        assert split_for_speech("") == []
        assert split_for_speech("   ") == []

    def test_sentences_become_separate_chunks(self):
        out = split_for_speech("Net worth is up eighteen hundred. Four creatives await your word.")
        assert out == ["Net worth is up eighteen hundred.",
                       "Four creatives await your word."]

    def test_a_single_sentence_stays_whole(self):
        text = "The oven is still on at one hundred and seventy five degrees."
        assert split_for_speech(text) == [text]

    # --- the seams that matter -------------------------------------------------

    def test_an_abbreviation_does_not_end_a_sentence(self):
        out = split_for_speech("Dr. Swayze called about it. It cleared.")
        assert out[0] == "Dr. Swayze called about it."
        assert len(out) == 2

    def test_a_decimal_number_is_not_a_sentence_boundary(self):
        out = split_for_speech("The charge was $63.41 in total. Nothing else moved.")
        assert "$63.41 in total." in out[0]
        assert len(out) == 2

    def test_a_single_initial_does_not_split(self):
        out = split_for_speech("It was signed J. Swayze this morning. Filed already.")
        assert out[0].startswith("It was signed J. Swayze")

    def test_question_and_exclamation_do_end_sentences(self):
        out = split_for_speech("Is the oven still on in the kitchen? "
                               "It is, and the door is closed! "
                               "The range is reading one hundred and seventy five.")
        assert len(out) == 3

    # --- shaping ---------------------------------------------------------------

    def test_a_very_short_chunk_is_merged_rather_than_spoken_alone(self):
        """Synthesising 'Yes.' on its own costs a whole model call for audio barely
        longer than the seam it introduces."""
        out = split_for_speech("Yes. The oven is still on and the door is closed.")
        assert len(out) == 1
        assert out[0].startswith("Yes.")

    def test_an_overlong_sentence_is_broken_at_a_clause_boundary(self):
        long = ("Simrig is up and available with no jobs queued, none running, seventeen "
                "done and none failed, and Gemma is loaded at eight point four gigabytes "
                "used with seven point six free, while ComfyUI is reachable too with "
                "image and video generation both ready to go right now.")
        out = split_for_speech(long)
        assert len(out) > 1
        # Every break lands after a comma/semicolon, never mid-phrase.
        for chunk in out[:-1]:
            assert chunk.rstrip()[-1] in ",;:.", f"bad seam: ...{chunk[-30:]!r}"

    def test_no_text_is_lost_or_invented_by_splitting(self):
        text = ("Net worth is up $1,840 on the week. The crypto book is green at +12.4%. "
                "Four creatives and one deploy are waiting on your word.")
        assert "".join(split_for_speech(text)).replace(" ", "") == text.replace(" ", "")

    def test_chunks_are_bounded_so_the_first_sound_comes_quickly(self):
        text = "word " * 400
        assert all(len(c) <= tts.MAX_CHUNK_CHARS + tts.MIN_CHUNK_CHARS
                   for c in split_for_speech(text))


class TestSynthesizeStream:
    """The streaming path, with Piper stubbed out -- these assert the orchestration,
    not the audio."""

    class FakeVoice:
        def __init__(self, fail_on=None):
            self.spoken, self.fail_on = [], fail_on

        def synthesize_wav(self, text, wav, syn_config=None):
            if self.fail_on and self.fail_on in text:
                raise RuntimeError("synthesis blew up")
            self.spoken.append(text)
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(22050)
            wav.writeframes(b"\x00\x00" * 100)

    def _speaker(self, voice):
        sp = Speaker("/nonexistent/voice.onnx")
        sp._voice = voice          # pre-loaded, so _ensure_loaded never touches disk
        return sp

    def test_each_sentence_arrives_as_its_own_playable_wav(self):
        voice = self.FakeVoice()
        sp = self._speaker(voice)
        chunks = list(sp.synthesize_stream(
            "The first sentence is comfortably long. "
            "The second sentence is also comfortably long. "
            "And the third one likewise runs past the merge threshold."))
        assert len(chunks) == 3
        # Self-contained WAVs, not raw PCM -- every client here can already play a WAV.
        assert all(c.startswith(b"RIFF") for c in chunks)

    def test_markdown_is_stripped_before_chunking(self):
        voice = self.FakeVoice()
        list(self._speaker(voice).synthesize_stream("**Bold** opening here. And a second one."))
        assert not any("*" in t for t in voice.spoken)

    def test_one_failed_chunk_does_not_silence_the_rest(self):
        """A reply that loses a sentence beats one that stops halfway."""
        voice = self.FakeVoice(fail_on="second")
        sp = self._speaker(voice)
        chunks = list(sp.synthesize_stream(
            "The first sentence is comfortably long. "
            "The second sentence is also comfortably long. "
            "And the third one likewise runs past the merge threshold."))
        assert len(chunks) == 2
        assert any("third" in t for t in voice.spoken)

    def test_nothing_to_say_is_an_error_not_silent_success(self):
        with pytest.raises(ValueError):
            list(self._speaker(self.FakeVoice()).synthesize_stream("   "))

    def test_the_blocking_path_still_returns_one_whole_wav(self):
        """synthesize() is unchanged for every existing caller."""
        voice = self.FakeVoice()
        out = self._speaker(voice).synthesize("The first sentence. The second sentence.")
        assert out.startswith(b"RIFF")
        assert len(voice.spoken) == 1, "the whole reply should be one synthesis call"


class TestOneVoiceEverywhere:
    """Jack had three voices: Orpheus on the phone, Piper on the terminals, and the
    browser's own speechSynthesis in the web UI -- which is a different voice on every
    browser and every OS. "I want it to feel as if Jarvis moved to the device im talking
    to him on." So the choice lives in Speaker and every surface asks it.

    The fallback is the part that has to be right. Preferring the slower, better voice is
    only safe because a failure lands on Piper instead of on silence.
    """

    def _speaker(self, monkeypatch, orpheus_result):
        """A real Speaker with both engines stubbed at their edges.

        Deliberately NOT a reimplementation of _synthesize_one: patching the method
        under test with a copy of itself proves only that the copy works. Only the two
        outermost calls are faked -- the HTTP post and the Piper model load -- so the
        real branching, the real fallback and the real error handling all execute.
        """
        spk = Speaker(voice_path="voice.onnx", orpheus_url="http://127.0.0.1:8130/tts")
        calls = {"orpheus": 0, "piper": 0}

        def fake_orpheus(spoken):
            calls["orpheus"] += 1
            if isinstance(orpheus_result, Exception):
                raise orpheus_result
            return orpheus_result

        class FakePiper:
            def synthesize_wav(self, spoken, wav, syn_config=None):
                calls["piper"] += 1
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(22050)
                wav.writeframes(bytes(16))

        monkeypatch.setattr(spk, "_orpheus", fake_orpheus)
        monkeypatch.setattr(spk, "_ensure_loaded", lambda: FakePiper())
        return spk, calls

    def test_the_natural_voice_is_used_when_it_is_there(self, monkeypatch):
        spk, calls = self._speaker(monkeypatch, b"ORPHEUS")
        assert spk.synthesize("Hello there.") == b"ORPHEUS"
        assert calls == {"orpheus": 1, "piper": 0}, "Piper must not also be invoked"

    def test_a_failure_falls_back_to_piper_rather_than_silence(self, monkeypatch):
        spk, calls = self._speaker(monkeypatch, RuntimeError("service down"))
        out = spk.synthesize("Hello there.")
        assert out.startswith(b"RIFF"), "should be the real WAV Piper produced"
        assert calls == {"orpheus": 1, "piper": 1}

    def test_every_chunk_of_a_long_reply_uses_the_same_engine(self, monkeypatch):
        """A reply that starts in one voice and finishes in another is worse than
        either voice on its own."""
        spk, calls = self._speaker(monkeypatch, b"ORPHEUS")
        chunks = list(spk.synthesize_stream(
            "The store is at zero. It has not moved in forty hours. "
            "Art director is the hold-up, and I already ran it once."))
        assert len(chunks) > 1 and set(chunks) == {b"ORPHEUS"}
        assert calls["piper"] == 0

    def test_with_no_natural_voice_configured_nothing_changes(self, monkeypatch):
        """Every surface behaved this way before Orpheus existed and must still."""
        spk, calls = self._speaker(monkeypatch, b"ORPHEUS")
        spk.orpheus_url = None
        assert spk.synthesize("Hello.").startswith(b"RIFF")
        assert calls == {"orpheus": 0, "piper": 1}

    def test_a_failure_with_no_fallback_raises_rather_than_returning_silence(self, monkeypatch):
        spk, _ = self._speaker(monkeypatch, RuntimeError("service down"))
        spk.voice_path = None
        with pytest.raises(RuntimeError):
            spk.synthesize("Hello.")

    def test_available_is_true_on_either_voice_alone(self):
        assert Speaker(voice_path="v.onnx").available() is True
        assert Speaker(orpheus_url="http://127.0.0.1:8130/tts").available() is True
        assert Speaker().available() is False

    def test_warming_is_a_no_op_without_the_natural_voice(self):
        assert Speaker(voice_path="v.onnx").warm() is False

    def test_warming_never_raises_when_the_service_is_down(self, monkeypatch):
        """It runs on a scheduler tick; an exception there kills the job for the life of
        the process."""
        spk = Speaker(orpheus_url="http://127.0.0.1:8130/tts")
        monkeypatch.setattr(spk, "_orpheus",
                            lambda s: (_ for _ in ()).throw(OSError("connection refused")))
        assert spk.warm() is False

    def test_warming_reports_success(self, monkeypatch):
        spk = Speaker(orpheus_url="http://127.0.0.1:8130/tts")
        monkeypatch.setattr(spk, "_orpheus", lambda s: b"WAV")
        assert spk.warm() is True

    def test_empty_audio_from_the_service_counts_as_a_failure(self, monkeypatch):
        """A 200 with no body would otherwise be handed to an audio player as silence,
        which is indistinguishable from Jarvis ignoring him."""
        import requests

        class Empty:
            content = b""
            def raise_for_status(self): pass

        monkeypatch.setattr(requests, "post", lambda *a, **k: Empty())
        spk = Speaker(orpheus_url="http://127.0.0.1:8130/tts")
        with pytest.raises(ValueError):
            spk._orpheus("Hello.")
