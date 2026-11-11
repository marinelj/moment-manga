"""The directed book: Claude directs and reviews (a fake Anthropic client here), Gemini draws (a fake too)."""
import json
import re
from types import SimpleNamespace

import pytest
from PIL import Image

from app import db, directed, director, pages, pipeline, settings
from test_batch_and_ready_email import FakeGemini, api, clip, needs_ffmpeg, store  # noqa: F401  (fixtures)


def art(direction="The lead (blue number 5) drives to screen-left.", **extra):
    return dict({"kind": "art", "t": 1.0, "box": [200, 300, 800, 700], "use_ref": True, "shows_lead": True,
                 "direction": direction, "caption": "Down nine.", "sfx": "DASH", "words": ""}, **extra)


def words(text="HE BIT."):
    return {"kind": "text", "t": 0, "box": [], "use_ref": False, "shows_lead": False, "direction": "",
            "caption": "", "sfx": "", "words": text}


def row(height, split, *cells):
    return {"height": height, "split": split, "cells": [{"panels": list(c)} for c in cells]}


LOCATE = {"gist": "A drive to the basket.", "on_screen_text": "7 - 16", "overlays": [[900, 0, 1000, 1000]],
          "track": [{"t": 0.0, "box": [300, 500, 700, 900]}, {"t": 2.5, "box": [200, 100, 700, 500]}],
          "hoop": [100, 100, 250, 220]}
PLAN = {"notes": ["0.00 blue 5 has the ball"],
        "facts": {"lead": "blue number 5", "events": [{"t": 1.0, "what": "pump fake"}], "ending": "goes in at 2.4 s",
                  "unclear": []},
        "title": "Down Nine",
        "cast": {"people": "every player is a 12-year-old boy", "lead": "blue number 5, pink shoes",
                 "kits": ["Blue team: royal blue, white numbers.", "Dark team: navy, red numbers."],
                 "others": ["Navy number 0 guards the lead."], "place": "maple floor, purple lines",
                 "ball": "The ball is dark grey."},
        "lead_views": [{"t": 0.5, "box": [300, 400, 700, 600]}, {"t": 1.5, "box": [300, 400, 700, 600]}],
        "pages": [
            {"rows": [row("normal", "even", [art()]),
                      row("tall", "left-wide", [art(sfx="The whistle!")], [art(use_ref=False, shows_lead=False), words()])]},
            {"rows": [row("tall", "even", [art(caption="")]), row("short", "even", [art(sfx="")])]}]}


def verdict(pid, **extra):
    return dict({"id": pid, "keep": "new", "verdict": "ok", "problems": [], "fix": "", "focus": [500, 400],
                 "corner": "tr", "sfx_at": [300, 800], "sfx_size": "large", "sfx_angle": -8, "sfx_colour": "yellow"},
                **extra)


class FakeDirector:
    """Answers like the Anthropic SDK's beta streaming helper: `with client.beta.messages.stream(...) as s`."""

    def __init__(self, reviews=(), plan=PLAN, stop_reason="end_turn", model="claude-fable-5-2"):
        self.reviews, self.plan, self.stop_reason, self.model = list(reviews), plan, stop_reason, model
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _answer(self, content):
        prompt = content[0]["text"]
        if prompt.startswith("These"):
            return LOCATE
        if prompt.startswith("Now the close study"):
            return self.plan
        ids = re.findall(r"Panel (D\d+-\d+)\.", " ".join(b.get("text", "") for b in content))
        flags = self.reviews.pop(0) if self.reviews else {}
        return {"panels": [verdict(pid, **flags.get(pid, {})) for pid in ids]}

    def _stream(self, **request):
        self.requests.append(request)
        answer = self._answer(request["messages"][0]["content"])
        message = SimpleNamespace(
            stop_reason=self.stop_reason, stop_details=SimpleNamespace(category="cyber"), model=self.model,
            content=[SimpleNamespace(type="thinking"), SimpleNamespace(type="text", text=json.dumps(answer))],
            usage=SimpleNamespace(input_tokens=50_000, output_tokens=5_000, cache_creation_input_tokens=0,
                                  cache_read_input_tokens=None))

        class Stream:
            def __enter__(self):
                return SimpleNamespace(get_final_message=lambda: message)

            def __exit__(self, *exc):
                return False

        return Stream()

    def kinds(self):
        return ["locate" if r["messages"][0]["content"][0]["text"].startswith("These") else
                "plan" if r["messages"][0]["content"][0]["text"].startswith("Now the close") else "review"
                for r in self.requests]


def order(clip, style="standard", lang="en"):
    return {"id": "order1", "video": str(clip), "theme": "sports", "style": "slamdunk", "lang": lang,
            "api_style": style}


def ledger():
    return [(line["step"], line["model"], line["usd"]) for line in db.order_costs("order1")]


# ---------- pages ----------

def test_page_shapes_are_slanted_fill_the_page_and_follow_reading_order():
    rows = [{"height": "normal", "split": "even", "cells": [1]},
            {"height": "tall", "split": "left-wide", "cells": [1, 2]}]
    shapes = pages.page_shapes(rows)
    assert len(shapes) == 4
    wide, tall, upper, lower = shapes
    assert wide[0][1] == wide[1][1] == pages.MARGIN and wide[3][1] != wide[2][1]      # level top, slanted bottom
    assert tall[2][1] == tall[3][1] == pages.PAGE_H - pages.MARGIN                    # level page bottom
    assert tall[1][0] < upper[0][0] and upper[2][1] < lower[1][1]                     # gutters between panels
    assert pages.ratio(wide) > 1.5 and pages.ratio(tall) < 1
    for shape in shapes:
        x0, y0, x1, y1 = pages.bounds(shape)
        assert pages.MARGIN <= x0 < x1 <= pages.PAGE_W - pages.MARGIN
        assert pages.MARGIN <= y0 < y1 <= pages.PAGE_H - pages.MARGIN
    mirrored = pages.page_shapes(rows, flip=True)
    assert mirrored[0][3][1] - mirrored[0][2][1] == -(wide[3][1] - wide[2][1])        # facing pages lean opposite ways


def test_build_page_letters_english_and_chinese(tmp_path):
    drawing = tmp_path / "a.png"
    Image.new("RGB", (1600, 900), (40, 90, 200)).save(drawing)
    shapes = pages.page_shapes([{"height": "normal", "split": "even", "cells": [1]},
                                {"height": "normal", "split": "even", "cells": [1, 1]}])
    page = pages.build_page([
        {"kind": "art", "shape": shapes[0], "art": drawing, "focus": (0.2, 0.5), "caption": "One hard dribble.",
         "corner": "br", "sfx": "WHOOSH", "sfx_at": (0.99, 0.99), "sfx_size": "large", "sfx_angle": 8},
        {"kind": "art", "shape": shapes[1], "art": drawing, "caption": "落后九分，球在五号手里。", "sfx": "唰"},
        {"kind": "text", "shape": shapes[2], "words": "HE BIT."}], number=2)
    assert page.size == (pages.PAGE_W, pages.PAGE_H)
    x0, y0, x1, y1 = pages.bounds(shapes[2])
    assert page.getpixel((x0 + 20, (y0 + y1) // 2)) == (0, 0, 0)                      # the text panel is black
    colours = page.crop(pages.bounds(shapes[0])).getcolors(1 << 20)
    assert any(c == (255, 255, 255) for _, c in colours)                              # caption box and SFX were drawn


# ---------- the director's calls ----------

def test_ask_sends_the_schema_effort_and_refusal_fallback_and_costs_the_answer(store):
    fake = FakeDirector()
    answer = director.locate(fake, "order1", [(0.0, Image.new("RGB", (64, 36)))], 3.0)
    assert answer["hoop"] == [100, 100, 250, 220]
    request = fake.requests[0]
    assert request["model"] == settings.DIRECTOR_MODEL == "claude-fable-5-2"
    assert request["betas"] == ["server-side-fallback-2026-07-01"] and request["fallbacks"] == "default"
    assert request["output_config"] == {"effort": settings.DIRECTOR_LOCATE_EFFORT,
                                        "format": {"type": "json_schema", "schema": director.LOCATE_SCHEMA}}
    assert "thinking" not in request and "tool_choice" not in request and "temperature" not in request
    picture = request["messages"][0]["content"][2]
    assert picture["type"] == "image" and picture["source"]["media_type"] == "image/jpeg"
    assert ledger() == [("director (locate)", "claude-fable-5-2", pytest.approx(50_000 * 8e-6 + 5_000 * 40e-6))]


def test_a_fallback_model_is_costed_at_its_own_price(store):
    director.locate(FakeDirector(model="claude-opus-4-8"), "order1", [(0.0, Image.new("RGB", (64, 36)))], 3.0)
    assert ledger() == [("director (locate)", "claude-opus-4-8", pytest.approx(50_000 * 5e-6 + 5_000 * 25e-6))]


def test_a_refusal_fails_the_call_instead_of_reading_an_empty_answer(store):
    with pytest.raises(director.DirectorError, match="declined"):
        director.locate(FakeDirector(stop_reason="refusal"), "order1", [(0.0, Image.new("RGB", (64, 36)))], 3.0)
    with pytest.raises(director.DirectorError, match="cut off"):
        director.locate(FakeDirector(stop_reason="max_tokens"), "order1", [(0.0, Image.new("RGB", (64, 36)))], 3.0)


def test_schemas_are_closed_objects_with_every_field_required():
    def walk(node):
        if node.get("type") == "object":
            assert node["additionalProperties"] is False and node["required"] == list(node["properties"])
            for child in node["properties"].values():
                walk(child)
        elif node.get("type") == "array":
            walk(node["items"])

    for schema in (director.LOCATE_SCHEMA, director.PLAN_SCHEMA, director.REVIEW_SCHEMA):
        walk(schema)


# ---------- the plan ----------

def test_normalise_keeps_only_what_pages_and_prompts_can_rely_on():
    times = [k / 10 for k in range(30)]
    rects = [(960, 270, 1920, 810)] * 30          # every zoom frame is the right half's middle
    raw = json.loads(json.dumps(PLAN))
    raw["pages"].append({"rows": [row("huge", "odd", [words(""), art(direction=" ")], [words("A"), words("B")])]})
    plan = directed.normalise(raw, LOCATE, times, rects, (1920, 1080), 3.0)
    panels = directed._panels(plan)
    assert [p["id"] for p in panels] == ["D1-1", "D1-2", "D1-3", "D1-4", "D2-5", "D2-6", "D3-7"]
    assert [p["kind"] for p in panels].count("text") == 2          # the third text panel is dropped
    assert plan["pages"][2]["rows"][0]["height"] == "normal" and plan["pages"][2]["rows"][0]["split"] == "even"
    first = panels[0]
    # [200, 300, 800, 700] on the zoom frame is this box on the whole frame (0-1000).
    assert first["box"] == [pytest.approx(350), pytest.approx(650), pytest.approx(650), pytest.approx(850)]
    assert panels[1]["sfx"] == "" and panels[1]["caption"] == "Down nine."     # no whistle in a silent clip
    assert panels[2]["box"] is None                                           # use_ref false: no reference crop
    assert len(plan["lead_views"]) == 2 and plan["overlays"] == [[900, 0, 1000, 1000]]
    with pytest.raises(director.DirectorError):
        directed.normalise({"pages": [{"rows": [row("tall", "even", [words()])]}]}, LOCATE, times, rects,
                           (1920, 1080), 3.0)


def test_panel_prompt_numbers_the_pictures_and_carries_the_standing_instructions():
    cast = PLAN["cast"]
    block = directed.style_block("slamdunk", cast)
    assert "full-colour" in block and "NO TEXT" in block and "12-year-old" in block and "Do not mirror" in block
    assert "The lead: blue number 5, pink shoes." in block and "Navy number 0" in block
    both = directed.panel_prompt({"direction": "Low angle."}, "3:4", block, True, True)
    assert both.startswith("Image 1 is the real video frame") and "Image 2 is the character sheet" in both
    assert "PANEL: Low angle." in both and "aspect ratio 3:4" in both and both.endswith(block)
    only_sheet = directed.panel_prompt({"direction": "Eyes."}, "21:9", block, False, True)
    assert "Image 1 is the character sheet" in only_sheet and "real video frame" not in only_sheet
    assert "full colour" in directed.style_block("naruto", cast)


# ---------- the whole book ----------

@needs_ffmpeg
def test_directed_book_end_to_end_with_an_edit_a_redraw_and_a_rejected_fix(store, clip, monkeypatch):
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "key")
    reviews = [{"D1-1": {"verdict": "edit", "fix": "remove the lettering on the wall", "problems": ["wall text"]},
                "D1-2": {"verdict": "redraw", "fix": "The lead moves to screen-LEFT.", "problems": ["mirrored"]}},
               {"D1-1": {"keep": "new"}, "D1-2": {"keep": "old", "verdict": "ok"}}]
    dc, gemini = FakeDirector(reviews), FakeGemini()
    progress = []
    pdf = pipeline.run(order(clip), progress=progress.append, client=_with(gemini, dc, monkeypatch),
                       sleep=lambda s: None)
    work = store / "jobs" / "order1"
    assert pdf == work / "manga.pdf" and pdf.exists() and (work / "page_2.jpg").exists()
    assert dc.kinds() == ["locate", "plan", "review", "review"] and progress == sorted(progress)
    assert gemini.storyboards == 0 and gemini.checks == 0                # no Gemini text model in this book
    assert gemini.images == 1 + 5 + 2                                    # sheet, five art panels, two fixes

    prompts = [c[-1] for _, c in gemini.calls]
    assert prompts[0].startswith("Image 1 is a strip of 2 zoomed pictures")
    assert sum("PANEL: The lead (blue number 5)" in p for p in prompts) == 6     # five panels and the redraw
    assert len(gemini.calls[1][1]) == 3 and len(gemini.calls[3][1]) == 1          # ref + sheet + words; words only
    edit = next(p for p in prompts if p.startswith("Edit image 1"))
    assert "change ONLY this: remove the lettering on the wall" in edit
    redraw = next(p for p in prompts if "CORRECTIONS" in p)
    assert redraw.rstrip().endswith("The lead moves to screen-LEFT.")

    second = dc.requests[3]["messages"][0]["content"]
    assert sum(b["type"] == "image" for b in second) == 1 + 2 * 3         # sheet; ref, old and new for two panels
    assert '"new" or "old"' in second[0]["text"] and "pump fake" in second[0]["text"]
    assert (work / "D1-1.fix1.png").exists() and (work / "D1-2.fix1.png").exists()
    final = directed.page_items(directed.panel_jobs(directed.normalise(
        PLAN, LOCATE, [0.0], [(0, 0, 640, 360)], (640, 360), 3.0), order(clip), clip, work, work / "sheet.png"),
        {"D1-1": "new", "D1-2": "old", "D1-3": "x", "D2-5": "x", "D2-6": "x"}, {"D1-1": verdict("D1-1")}, 0)
    assert final[0]["corner"] == "tr" and final[0]["sfx_at"] == (0.3, 0.8) and final[0]["sfx_colour"] == "yellow"
    assert final[3] == {"kind": "text", "shape": final[3]["shape"], "words": "HE BIT."}

    steps = [step for step, _, _ in ledger()]
    assert steps.count("book panel") == 5 and steps.count("character sheet") == 1
    assert steps.count("panel fix") == 1 and steps.count("panel redraw") == 1
    assert [s for s in steps if s.startswith("director")] == [
        "director (locate)", "director (plan)", "director (review)", "director (review of fixes 1)"]

    # A restarted order walks the same steps from its folder and pays for nothing twice.
    pipeline.run(order(clip), client=gemini, sleep=lambda s: None)
    assert len(dc.requests) == 4 and gemini.images == 8 and len(ledger()) == len(steps)


def _with(gemini, dc, monkeypatch):
    monkeypatch.setattr(director, "client", lambda: dc)
    return gemini


@needs_ffmpeg
def test_directed_batch_order_sends_its_own_pictures_words_and_size(store, clip, monkeypatch):
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "key")
    dc, gemini = FakeDirector(), FakeGemini(missing=0, errors=0)
    pipeline.run(order(clip, "batch"), client=_with(gemini, dc, monkeypatch), sleep=lambda s: None, now=lambda: 0)
    assert gemini.created == 1 and [r["key"] for r in gemini.requests] == ["D1-1", "D1-2", "D1-3", "D2-5", "D2-6"]
    request = gemini.requests[0]["request"]
    assert request["generationConfig"]["imageConfig"] == {"aspectRatio": "16:9", "imageSize": "2K"}
    parts = request["contents"][0]["parts"]
    assert [list(p) for p in parts] == [["inlineData"], ["inlineData"], ["text"]]
    assert parts[2]["text"].startswith("Image 1 is the real video frame")
    assert len(gemini.requests[2]["request"]["contents"][0]["parts"]) == 1          # a panel with words only
    assert [s for s, _, _ in ledger()].count("book panel (batch)") == 5 and gemini.images == 1


@needs_ffmpeg
def test_without_a_key_or_when_switched_off_the_gemini_only_book_runs(store, clip, monkeypatch):
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", None)
    gemini = FakeGemini()
    pipeline.run(order(clip), client=gemini, sleep=lambda s: None)
    assert gemini.storyboards == 1 and (store / "jobs" / "order1" / "plan.json").exists()
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "key")
    monkeypatch.setattr(settings, "BOOK_DIRECTOR", "off")
    assert not directed.enabled()
    monkeypatch.setattr(settings, "BOOK_DIRECTOR", "auto")
    assert directed.enabled()


def test_admin_status_says_who_directs_the_book(api, monkeypatch):
    client = api
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", None)
    status = client.get("/api/admin/status").json()
    assert status["book_director"] == "gemini" and status["anthropic_key_set"] is False
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "key")
    status = client.get("/api/admin/status").json()
    assert status["book_director"] == "claude" and status["director_model"] == "claude-fable-5-2"
    assert "key" not in json.dumps(status["director_model"])


def test_a_rejected_request_feature_is_dropped_and_the_call_goes_again(store):
    import anthropic
    import httpx2

    class Picky(FakeDirector):
        def _stream(self, **request):
            reply = httpx2.Response(400, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
            if "fallbacks" in request:
                self.requests.append(request)
                raise anthropic.BadRequestError("fallbacks: this model has no server-side fallback",
                                                response=reply, body=None)
            if "format" in request["output_config"]:
                self.requests.append(request)
                raise anthropic.BadRequestError("output_config.format: schema is too complex", response=reply,
                                                body=None)
            return super()._stream(**request)

    fake = Picky()
    answer = director.locate(fake, "order1", [(0.0, Image.new("RGB", (64, 36)))], 3.0)
    assert answer["gist"] == "A drive to the basket." and len(fake.requests) == 3
    last = fake.requests[-1]
    assert "fallbacks" not in last and "betas" not in last and "format" not in last["output_config"]
    assert "must follow this JSON schema" in last["messages"][0]["content"][-1]["text"]
    assert director._json('```json\n{"a": 1}\n```') == {"a": 1}
    assert len(ledger()) == 1                                    # only the call that reached the model is costed
