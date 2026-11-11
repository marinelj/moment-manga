"""The Grok bot as the book's director: questions travel as a private bundle it downloads (a fake gateway here)."""
import io
import json
import re
import zipfile

import pytest
from PIL import Image

from app import db, directed, director, pipeline, settings
from test_batch_and_ready_email import FakeGemini, api, clip, needs_ffmpeg, store  # noqa: F401  (fixtures)
from test_directed_book import LOCATE, PLAN, ledger, order, verdict

GATEWAY = "https://gateway.example/grokbot"
BUNDLES = "https://shop.example/api/director/bundles"


class Reply:
    def __init__(self, body, status=200):
        self.body, self.status = body, status

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    def json(self):
        return self.body


class FakeGateway:
    """Stands in for the Grok bot's gateway and for the bot: each run opens the bundle named in its prompt the
    way the bot would, and answers from task.md. `script` overrides the outcome of successive runs."""

    def __init__(self, script=(), reviews=()):
        self.script, self.reviews = list(script), list(reviews)
        self.prompts, self.tasks, self.pictures, self.keys, self.runs = [], [], [], [], {}

    def post(self, url, json=None, headers=None):
        assert url == GATEWAY + "/v1/runs" and set(json) == {"prompt"}
        self.prompts.append(json["prompt"])
        self.keys.append(headers["Idempotency-Key"])
        token = re.search(re.escape(BUNDLES) + r"/([\w-]+)/bundle\.zip", json["prompt"]).group(1)
        with zipfile.ZipFile(settings.DIRECTOR_BUNDLE_DIR / token / "bundle.zip") as archive:
            task = archive.read("task.md").decode()
            self.pictures.append(sorted(n for n in archive.namelist() if n.endswith(".jpg")))
            Image.open(io.BytesIO(archive.read("001.jpg"))).verify()
        self.tasks.append(task)
        outcome = self.script.pop(0) if self.script else "ok"
        run = f"run{len(self.prompts)}"
        if outcome == "ok":
            self.runs[run] = {"status": "completed", "final_output": json_text(self._answer(task))}
        elif outcome == "lost":
            self.runs[run] = {"status": "failed", "error": "Webhook delivery could not be confirmed."}
        elif outcome == "prose":
            self.runs[run] = {"status": "completed", "final_output": "I looked at the frames. It is a drive."}
        elif outcome == "partial":
            self.runs[run] = {"status": "completed", "final_output": '```json\n{"gist": "A drive."}\n```'}
        else:
            self.runs[run] = {"status": outcome}
        return Reply({"run_id": run, "status": "queued"})

    def get(self, url):
        return Reply(self.runs[url.rsplit("/", 1)[1]])

    def _answer(self, task):
        if "These 1 frames" in task or "show the whole picture of the clip" in task:
            return LOCATE
        if "Now the close study" in task:
            return dict(PLAN, notes=["a note"] * task.count("Zoom frame at t = "))
        flags = self.reviews.pop(0) if self.reviews else {}
        return {"panels": [verdict(pid, **flags.get(pid, {})) for pid in re.findall(r"Panel (D\d+-\d+)\.", task)]}


def json_text(answer):
    return "Here is the answer.\n" + json.dumps(answer)       # a stray sentence around the JSON is tolerated


@pytest.fixture
def grok(store, monkeypatch):
    monkeypatch.setattr(settings, "BOOK_DIRECTOR", "grok")
    monkeypatch.setattr(settings, "GROK_DIRECTOR_URL", GATEWAY)
    monkeypatch.setattr(settings, "DIRECTOR_BUNDLE_DIR", store / "director_bundles")
    monkeypatch.setattr(settings, "DIRECTOR_BUNDLE_URL", BUNDLES)
    return store


def frames():
    return [(0.0, Image.new("RGB", (64, 36), "navy"))]


def test_a_question_travels_as_a_private_bundle_and_the_bundle_is_gone_afterwards(grok):
    gateway = FakeGateway()
    answer = director.locate(director.GrokDirector(gateway), "order1", frames(), 3.0, sleep=lambda s: None)
    assert answer == LOCATE
    prompt, task = gateway.prompts[0], gateway.tasks[0]
    assert len(prompt) < 600 and "task.md" in prompt             # long prompts get lost on the way to the bot
    assert gateway.pictures == [["001.jpg"]]
    assert director.SYSTEM in task and "[picture 001.jpg]" in task and "Frame at t = 0.00 s:" in task
    assert json.dumps(director.LOCATE_SCHEMA, indent=1) in task
    assert not any((grok / "director_bundles").iterdir())
    assert ledger() == [("director (locate)", "grok-bot", 0.0)]


def test_a_lost_delivery_and_a_broken_answer_go_again(grok):
    gateway = FakeGateway(script=["lost", "prose", "ok"])
    assert director.locate(director.GrokDirector(gateway), "order1", frames(), 3.0, sleep=lambda s: None) == LOCATE
    assert len(gateway.prompts) == 3 and len(set(gateway.prompts)) == 1 and len(set(gateway.keys)) == 3

    gateway = FakeGateway(script=["partial"] * 3)
    with pytest.raises(director.DirectorError, match="lacks on_screen_text"):
        director.locate(director.GrokDirector(gateway), "order1", frames(), 3.0, sleep=lambda s: None)
    assert not any((grok / "director_bundles").iterdir())


def test_a_run_that_timed_out_is_not_sent_again(grok, monkeypatch):
    gateway = FakeGateway(script=["timed_out"])
    with pytest.raises(director.DirectorError, match="ran out of time"):
        director.locate(director.GrokDirector(gateway), "order1", frames(), 3.0, sleep=lambda s: None)
    assert len(gateway.prompts) == 1

    monkeypatch.setattr(settings, "GROK_DIRECTOR_TIMEOUT", 30)
    waits = []
    gateway = FakeGateway(script=["running"])
    with pytest.raises(director.DirectorError, match="did not answer"):
        director.locate(director.GrokDirector(gateway), "order1", frames(), 3.0, sleep=waits.append)
    assert waits == [10, 10, 10] and len(gateway.prompts) == 1


def test_grok_must_be_set_up_before_it_directs(grok, monkeypatch):
    monkeypatch.setattr(settings, "GROK_DIRECTOR_URL", "")
    assert director.provider() == "grok" and directed.enabled()     # chosen by name: the order fails, loudly
    with pytest.raises(RuntimeError, match="GROK_DIRECTOR_URL"):
        director.client()


def test_the_bundle_address_works_only_while_the_question_is_open(api, grok):
    folder, url = director.bundle([director.text("Look."), director.image(frames()[0][1])], director.LOCATE_SCHEMA)
    path = url.removeprefix("https://shop.example")
    assert re.fullmatch(r"/api/director/bundles/[\w-]{32}/bundle\.zip", path)
    got = api.get(path)
    assert got.status_code == 200 and got.headers["cache-control"] == "no-store"
    assert zipfile.ZipFile(io.BytesIO(got.content)).namelist() == ["001.jpg", "task.md"]
    assert api.get(path.replace(folder.name, "x" * 32)).status_code == 404
    assert api.get(f"/api/director/bundles/{folder.name}/task.md").status_code == 404
    assert api.get("/api/director/bundles/../bundle.zip").status_code == 404
    folder.joinpath("bundle.zip").unlink()
    assert api.get(path).status_code == 404


def test_the_admin_page_chooses_the_director(api, monkeypatch):
    monkeypatch.setattr(settings, "BOOK_DIRECTOR", "auto")
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", None)
    monkeypatch.setattr(settings, "GROK_DIRECTOR_URL", GATEWAY)
    status = api.get("/api/admin/status").json()
    assert (status["book_director"], status["book_director_choice"]) == ("gemini", "auto")
    assert status["book_director_source"] == "BOOK_DIRECTOR setting" and status["grok_director_url_set"] is True

    assert api.post("/api/admin/book-director", json={"director": "grok"}).json() == {
        "book_director": "grok", "book_director_choice": "grok"}
    status = api.get("/api/admin/status").json()
    assert (status["book_director"], status["director_model"]) == ("grok", "grok-bot")
    assert status["book_director_source"] == "admin page"

    assert api.post("/api/admin/book-director", json={"director": "off"}).json()["book_director"] == "gemini"
    assert api.post("/api/admin/book-director", json={"director": "codex"}).status_code == 400
    assert api.post("/api/admin/book-director", json={"director": None}).json()["book_director_choice"] == "auto"


@needs_ffmpeg
def test_a_book_directed_by_grok_end_to_end(grok, clip, monkeypatch):
    reviews = [{"D1-1": {"verdict": "edit", "fix": "remove the lettering on the wall", "problems": ["wall text"]}},
               {"D1-1": {"keep": "new"}}]
    gateway, gemini = FakeGateway(reviews=reviews), FakeGemini()
    monkeypatch.setattr(director, "client", lambda: director.GrokDirector(gateway))
    pdf = pipeline.run(order(clip), client=gemini, sleep=lambda s: None)
    work = grok / "jobs" / "order1"
    assert pdf == work / "manga.pdf" and pdf.exists()
    assert len(gateway.prompts) == 4                              # locate, plan, review, review of the fix
    assert len(gateway.pictures[1]) > 20 and "Zoom frame at t = " in gateway.tasks[1]
    assert "Panel D1-1. Your direction:" in gateway.tasks[2] and '"new" or "old"' in gateway.tasks[3]
    assert gemini.storyboards == 0 and gemini.images == 1 + 5 + 1  # sheet, five art panels, one fix
    assert [(s, m, usd) for s, m, usd in ledger() if s.startswith("director")] == [
        ("director (locate)", "grok-bot", 0.0), ("director (plan)", "grok-bot", 0.0),
        ("director (review)", "grok-bot", 0.0), ("director (review of fixes 1)", "grok-bot", 0.0)]
    assert not any((grok / "director_bundles").iterdir())
    assert db.order_costs("order1")
