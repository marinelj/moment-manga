"""The book's director: Claude studies the clip's frames, writes down what really happens, directs every panel
for the image model, and reviews every drawing.

The Gemini-only book (app/pipeline.py) asked a small model what happens in a wide gym shot where players are
about 110 px tall, then traced whole frames: the stories were partly invented and the panels lifeless. A book
directed by hand with this method (zoomed frames ten a second, facts first, re-staged panels, a review of every
drawing) was the first one the owner accepted, so this module does that through the Anthropic API.

Three kinds of call, all answered as JSON that follows a schema:
  locate  whole frames, a few a second: where the play is over time, the on-screen graphics, the basket.
  plan    zoom frames that follow the play: notes per frame, the facts, the cast, and the pages with a
          direction for every panel.
  review  the drawings next to their directions and references: ok, edit or redraw, and where the lettering goes.

ANTHROPIC_API_KEY, DIRECTOR_MODEL and DIRECTOR_EFFORT come from the environment (see settings.py).

The same three questions can go to the Grok bot instead (BOOK_DIRECTOR=grok or the admin page's choice): see
GrokDirector below. The prompts, the schemas and everything in directed.py stay the same; only the way a question
travels differs.
"""
import base64
import json
import secrets
import shutil
import time
import uuid
import zipfile
from io import BytesIO
from pathlib import Path

from PIL import Image

from . import db, modes, settings

# Server-side refusal fallback: if a safety classifier declines a request, Anthropic re-runs it on the model
# it recommends for that case inside the same call, instead of the order failing on a false positive.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# USD per million tokens (input, output incl. thinking), list prices of 2026-11. A fallback model answers at its
# own price; a model not listed here is costed at DIRECTOR_PRICE_IN / DIRECTOR_PRICE_OUT.
PRICES = {"claude-fable-5-2": (8.0, 40.0), "claude-opus-5-5": (4.0, 20.0), "claude-opus-5": (5.0, 25.0),
          "claude-opus-4-8": (5.0, 25.0)}


class DirectorError(RuntimeError):
    """The director could not answer (refused, cut off, or not JSON). The order fails and can be queued again:
    everything already paid for is saved in its folder."""


SYSTEM = """You are the director of a short manga book made from one real video clip that a customer uploaded, \
usually a basketball play from a youth game filmed from the stands. You work with an illustrator: an image model \
that draws well but cannot see the video. It draws what your words say, helped by a small blurry reference picture. \
You study the frames, write down what really happens, direct each panel, and later check the drawings.

The customer pays for their own real moment, told with the energy of a sports manga. Two things decide whether the \
book is worth anything to them.

It is true. Every event, who did it, with which hand, in which direction, and how it ended, is as in the clip. \
Directions are as seen on screen (screen-left, screen-right) and never mirrored. Nothing is invented: no fouls, \
whistles, referee's calls, celebrations, crowd reactions, scores or names that the frames do not show. What cannot \
be seen is called unclear and stays out of the story.

It is alive. The camera in the clip is far away and the players are tiny, so a panel that traces the frame is \
lifeless. You re-stage each moment the way a manga artist would: close-ups, low angles, foreshortening, a big panel \
for the climax. The staging is yours; the facts are the clip's.

People are described by what they wear (kit colour, number, shoes, sleeves, hair), never by guessing who they \
are. Ages are described as they look, and children are drawn as children."""

LOCATE_PROMPT = """These {n} frames show the whole picture of the clip, in order ({dur:.1f} s long); each is \
labelled with its time. This first pass only finds where things are, so the next pass can zoom in. Who does what \
is decided there, from the zooms, so don't spend effort on it here.

- gist: two or three sentences on what the clip shows.
- on_screen_text: what the score bar, captions and banners say (league, team names, score, period) and whether the \
score changes during the clip; "" if there is none.
- overlays: boxes around graphics drawn on top of the picture (score bar, channel logo, captions).
- track: for every frame, its time and one box around the main play at that time: the player with the ball and \
the opponents and team-mates within reach. Work backwards. First find the decisive moment (usually a shot at a \
basket) and the person who makes it: that is the lead. Then follow the lead and the ball through every earlier \
and later frame. The camera is usually far away, so the play is often among the smallest, most distant figures, \
while larger players near the camera only stand and watch: never box people because they are large or clear, box \
where the ball is. Before the lead has the ball, box whoever has it; if the ball cannot be found in a frame, box \
the lead and the people crowding the lead. While a shot or a pass is in the air, stretch the box so the ball and \
its target are inside. The next pass sees only what is inside these boxes plus a margin, so keep the play inside.
- hoop: one box around the basket (backboard and rim) that the play attacks, or [] if there is none.

Boxes are [ymin, xmin, ymax, xmax] on a 0-1000 scale of the frame."""

PLAN_PROMPT = """Now the close study. The {n} zoom frames below follow the play through the clip ({dur:.1f} s), \
{fps:g} a second. Each is labelled with its time and shows only a part of the whole picture.{whole_note}{hoop_note}
From the first pass, about the whole picture: {context}

Step 1, notes. For every zoom frame write one short note, starting with its time: who has the ball (kit colour \
and number, or "in the air", "in the net", "loose", "hidden", "unclear") and what the lead's body is doing (feet, \
hands, direction of travel on screen). Fast moves last two or three frames, so compare each frame with its \
neighbours: that is how a pump fake, a crossover or a change of hands is told apart from a shot.

Step 2, facts. From the notes: who the lead is (the person who makes the main play); the events in order with \
their times and the exact technique (in basketball for example: pump fake, crossover, behind-the-back dribble, \
pull-up jump shot, floater, one-foot layup with the right or the left hand, off the glass or straight in); and the \
ending (whether the shot goes in, and when the ball is in the net). Decide made or missed only from frames in \
which you can see the ball at the rim. List under "unclear" anything that matters but cannot be seen.

Step 3, cast and place. These become the illustrator's standing instructions, sent with every panel.
- people: one sentence on who the players are and how old they look, for example "every player is a 12-year-old \
boy: small, slim, child proportions, soft young faces, no adult muscles, no facial hair; black hair".
- lead: the lead's look in one line: kit colour and number, hair, build, shoes, anything that sets them apart.
- kits: one line per team (jersey and shorts colours, trim, the colour of the numbers, undershirts), and one for \
the referee if one is seen.
- others: one line per other person who appears in a panel, with what sets them apart (number, shoes, sleeves, \
knee pads, height).
- place: the floor, lines, walls and backboard in plain colours and shapes. Describe signs and banners as plain \
shapes without their wording, because the illustrator copies any word it reads onto the wall.
- ball: its colour.
- lead_views: the three or four zoom frames in which the lead is seen largest and clearest, from different sides \
if possible; each with its time label and a tight box around the lead's whole body. They become the reference for \
the lead's character sheet.

Step 4, the book: {tone}. Plan 3 or 4 pages. A page is rows from top to bottom. A row is "tall", "normal" or \
"short" and holds one cell (a wide panel; write "even" for its split) or two cells side by side (split "even", \
"left-wide" or "right-wide"). A cell holds one panel, or two panels stacked. Three to five panels a page, 9 to 14 \
in the book. A single panel in a tall row beside a stack of two makes a strong page: the big vertical moment with \
two quick beats next to it. Re-stage every moment; do not describe the frame as it was filmed. At most one panel \
in the book is a wide view of the court. Every other art panel is a close or medium shot built around one or two \
people, usually the lead and the nearest opponent, from an angle the far camera never had (low from the floor, \
over a defender's shoulder, from under the basket, tight on the hands and the ball), and it leaves out everyone \
who only stands and watches. Give the opening view a wide panel, the decisive move and the finish the biggest \
panels, and quick beats (eyes, feet, the ball on the glass, the net) the small ones. Tell only the events of \
step 2, in order, and end where the clip ends. If the result of the shot is unclear, end with the ball in the air.

Each panel:
- kind: "art", or "text" for a black panel with one to three big words in "words" (at most two in the book; they \
mark a beat, such as the instant a defender is fooled).
- t and box: the time label of the zoom frame that shows this moment, and a box on that frame around the people \
in the panel. The illustrator gets this crop as a blurry reference for positions and poses. Set use_ref false \
only for a panel that needs no reference, such as a close-up of the lead's eyes.
- shows_lead: true if the lead is in the panel; the character sheet is then sent along.
- direction: 50 to 110 words to the illustrator, who knows nothing but your words, the reference crop, the \
character sheet and the standing instructions. Give the kind of shot and the camera angle; who is in the panel, by \
kit colour and number, and where each stands (screen-left or screen-right, front or back); what each body does \
(which hand, which foot, on or off the floor, looking where); exactly where the ball is; what is behind them. \
When the direction of travel matters, say which way the lead moves and which part of the body leads, for example \
"moving to screen-left: head and chest are the leftmost part of the body, the trailing foot is at screen-right". \
Call the lead "the lead" and give the number too. Name every number that may appear, because otherwise the \
illustrator repeats numbers and gives the lead's shoes to other players. Ask for no writing in the picture, and \
use no nicknames and no team names.
- caption: narration in {lang}, at most 60 characters, or "". It tells the event with a pulse, the way a sports \
manga narrates ("Trapped.", "One dribble. Gone.", "Off the glass..."), not as a description of the picture; two \
to six words work best. It may also give what the on-screen text states (league, score). It invents nothing. Not \
every panel needs one.
- sfx: one short sound-effect word in {lang}, or "". Never a whistle, a foul or a call.

Boxes are [ymin, xmin, ymax, xmax] on a 0-1000 scale of the zoom frame they belong to. The lettering is added \
afterwards by us, so the drawings themselves carry no text. For a text panel, leave the art fields empty."""

WHOLE_NOTE = " Before them come {n} whole frames, for orientation only."
HOOP_NOTE = (" After them come close-ups of the basket at the same moments, as sheets of small tiles; each tile is "
             "labelled with its time in seconds. Use them to see whether the ball goes through the net.")

REVIEW_PROMPT = """The illustrator has drawn {n} panels of the book. The lead's character sheet comes first. \
Then, for each panel, you get your direction, the reference crop it was drawn from (if it had one) and the \
drawing.{compare_note}

The facts of the clip: {facts}
The standing instructions the illustrator had: {style}

Judge each drawing as the customer would, and as the printer must.
- Truth: the event, the pose, the hands, the ball's place, who is where and the direction of travel match your \
direction. A scene drawn mirrored, the lead moving the wrong way, or a made shot drawn as a miss is wrong, however \
good it looks.
- Cast: kit colours and numbers are right; no number is worn by two people; the lead matches the sheet (hair, \
kit, number, shoes) and nobody else wears the lead's number or shoes; no extra main figure who is not in your \
direction; children look their age.
- Clean: no letters, words, logos, sound effects or symbols drawn anywhere except jersey numbers (look for small \
Japanese characters and for signs on walls); one ball only; no extra limbs or fused bodies; the same colour \
treatment as the other panels.
Small flaws a reader would not notice are fine. Each fix costs another drawing, so ask for one only where it makes \
the book better.

For each panel, in the order given, with its id:
- keep: {keep_rule}
- verdict: "ok"; or "edit" when the picture is right but something local must change (lettering to remove, a \
stray ball, a wrong number or shoes, a face that looks too old); or "redraw" when the staging itself is wrong \
(wrong pose, wrong direction, wrong people).
- problems: what is wrong, shortly; [] if ok.
- fix: for "edit", one instruction naming exactly what to change and what to put in its place (everything else is \
kept as it is). For "redraw", the sentences to add to your direction so that the next drawing comes out right. \
"" if ok.
Then the lettering for the drawing you keep. Points are [x, y] on a 0-1000 scale of that drawing, x from the left \
and y from the top.
- focus: the point that must stay near the middle when the drawing is cropped to its panel shape, usually between \
the lead's head and the ball.
- corner: the corner for the caption box (tl, tr, bl, br) where it covers no face, hand or ball.
- sfx_at: where the centre of the sound effect goes: on open floor, wall or air near the action, away from faces \
and the ball. sfx_size "small", "medium" or "large"; sfx_angle between -12 and 12 degrees; sfx_colour "white", or \
"yellow" for the biggest moment of the book."""

COMPARE_NOTE = (" Some panels come with two drawings: the first one, which you flagged (\"old\"), and a fix "
                "(\"new\"). Keep the better of the two and judge the one you keep.")

_BOX = {"type": "array", "items": {"type": "number"}}


def _obj(**props) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def _list(items: dict) -> dict:
    return {"type": "array", "items": items}


_STR, _NUM, _BOOL = {"type": "string"}, {"type": "number"}, {"type": "boolean"}

LOCATE_SCHEMA = _obj(gist=_STR, on_screen_text=_STR, overlays=_list(_BOX),
                     track=_list(_obj(t=_NUM, box=_BOX)), hoop=_BOX)
_PANEL = _obj(kind={"type": "string", "enum": ["art", "text"]}, t=_NUM, box=_BOX, use_ref=_BOOL, shows_lead=_BOOL,
              direction=_STR, caption=_STR, sfx=_STR, words=_STR)
PLAN_SCHEMA = _obj(
    notes=_list(_STR),
    facts=_obj(lead=_STR, events=_list(_obj(t=_NUM, what=_STR)), ending=_STR, unclear=_list(_STR)),
    title=_STR,
    cast=_obj(people=_STR, lead=_STR, kits=_list(_STR), others=_list(_STR), place=_STR, ball=_STR),
    lead_views=_list(_obj(t=_NUM, box=_BOX)),
    pages=_list(_obj(rows=_list(_obj(
        height={"type": "string", "enum": ["tall", "normal", "short"]},
        split={"type": "string", "enum": ["even", "left-wide", "right-wide"]},
        cells=_list(_obj(panels=_list(_PANEL))))))))
REVIEW_SCHEMA = _obj(panels=_list(_obj(
    id=_STR, keep={"type": "string", "enum": ["new", "old"]},
    verdict={"type": "string", "enum": ["ok", "edit", "redraw"]}, problems=_list(_STR), fix=_STR,
    focus=_BOX, corner={"type": "string", "enum": ["tl", "tr", "bl", "br"]}, sfx_at=_BOX,
    sfx_size={"type": "string", "enum": ["small", "medium", "large"]}, sfx_angle=_NUM,
    sfx_colour={"type": "string", "enum": ["white", "yellow"]})))


def provider() -> str | None:
    """Who directs a real book now: "claude", "grok", or None for the Gemini-only book. A director that was
    chosen by name but is not set up still counts: its orders fail with the reason instead of quietly getting
    the Gemini-only book."""
    choice = modes.book_director()
    if choice in ("claude", "grok"):
        return choice
    return "claude" if choice == "auto" and settings.ANTHROPIC_API_KEY else None


def enabled() -> bool:
    return provider() is not None


def client():
    if provider() == "grok":
        return GrokDirector()
    import anthropic
    if not settings.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    return anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, max_retries=4)


def image(source, longest: int | None = None) -> dict:
    """A picture (a path or a PIL image) as a content block: a JPEG no longer than `longest` on its long side.
    Quality 82 keeps a study of a hundred zoom frames near 6 MB, which matters on a slow uplink."""
    if isinstance(source, (str, Path)):
        with Image.open(source) as opened:
            img = opened.convert("RGB")
    else:
        img = source.convert("RGB")
    if longest and max(img.size) > longest:
        img.thumbnail((longest, longest))
    buf = BytesIO()
    img.save(buf, "JPEG", quality=82)
    data = base64.standard_b64encode(buf.getvalue()).decode()
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}


def text(words: str) -> dict:
    return {"type": "text", "text": words}


def _cost(oid: str | None, step: str, message) -> None:
    usage = getattr(message, "usage", None)
    if not (oid and usage):
        return
    model = str(getattr(message, "model", None) or settings.DIRECTOR_MODEL)
    price_in, price_out = next((p for name, p in PRICES.items() if model.startswith(name)),
                               (settings.DIRECTOR_PRICE_IN, settings.DIRECTOR_PRICE_OUT))
    tokens_in = sum(getattr(usage, k, 0) or 0 for k in ("input_tokens", "cache_creation_input_tokens",
                                                       "cache_read_input_tokens"))
    tokens_out = getattr(usage, "output_tokens", 0) or 0
    db.add_cost(oid, step, model, tokens_in + tokens_out, (tokens_in * price_in + tokens_out * price_out) / 1e6)


def _json(answer: str):
    """The JSON object in an answer; without the schema feature the model may wrap it in a code fence."""
    start, end = answer.find("{"), answer.rfind("}")
    return json.loads(answer[start:end + 1] if 0 <= start < end else answer)


def ask(dc, oid: str | None, step: str, content: list, schema: dict, effort: str | None = None,
        sleep=time.sleep) -> dict:
    """One question to the director, answered as JSON that follows `schema`. Streamed, because a long answer
    behind a quiet connection gets cut on the way. Every attempt that reached the model is costed.

    Two request features may be unavailable to an account or a model: the answer schema and the refusal
    fallback. If the API rejects the request naming one of them, the call goes again without it: the schema
    then travels in the prompt, and a refusal then fails the order."""
    if isinstance(dc, GrokDirector):
        return dc.ask(oid, step, content, schema, sleep)
    import anthropic
    strict, fallback, last = True, True, None
    for attempt in range(5):
        request = {"model": settings.DIRECTOR_MODEL, "max_tokens": settings.DIRECTOR_MAX_TOKENS, "system": SYSTEM,
                   "output_config": {"effort": effort or settings.DIRECTOR_EFFORT},
                   "messages": [{"role": "user", "content": content}]}
        if strict:
            request["output_config"]["format"] = {"type": "json_schema", "schema": schema}
        else:
            request["messages"][0]["content"] = content + [text(
                "Answer with one JSON object and nothing else. It must follow this JSON schema:\n"
                + json.dumps(schema))]
        if fallback:
            request.update(betas=[FALLBACK_BETA], fallbacks="default")
        try:
            with dc.beta.messages.stream(**request) as stream:
                message = stream.get_final_message()
        except anthropic.BadRequestError as exc:
            words = str(exc).lower()
            if fallback and "fallback" in words:
                fallback = False
            elif strict and any(w in words for w in ("schema", "output_config.format", "structured output")):
                strict = False
            else:
                raise
            last = exc
            continue
        except (anthropic.APIConnectionError, anthropic.RateLimitError, anthropic.InternalServerError) as exc:
            last = exc                 # the SDK already retried; wait longer and try the whole call again
            sleep(10 * 2 ** attempt)
            continue
        _cost(oid, step, message)
        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            raise DirectorError(f"The director declined the {step} request "
                                f"(category: {getattr(details, 'category', None)})")
        if message.stop_reason == "max_tokens":
            raise DirectorError(f"The director's {step} answer was cut off at DIRECTOR_MAX_TOKENS")
        answer = "".join(block.text for block in message.content if getattr(block, "type", "") == "text")
        try:
            data = _json(answer)
        except ValueError as exc:
            last = exc
            continue
        if isinstance(data, dict):
            return data
        last = ValueError("not a JSON object")
    raise DirectorError(f"The director gave no usable {step} answer: {last}")


GROK_TASK = """# Your role

{system}

# How to work

This folder holds {n} pictures, 001.jpg to {last}. In the task below a line such as "[picture 007.jpg]" stands \
for that picture, and the words just before it say what it is.

Open every picture and look at it with your own eyes, in the order given. Never judge a picture from its file \
name, from a script or from a guess, and skip none: the task is about small differences between neighbouring \
pictures. With many pictures, write short notes to a file as you go, so that nothing is lost by the time you \
write the answer.

# The task

{body}

# Your answer

Reply with one JSON object and nothing else: no words before or after it and no code fence. It must follow this \
JSON schema, with every field present:

{schema}
"""
GROK_PROMPT = ("From the Moment Manga backend, on behalf of its owner. You are the director of a manga book made "
               "from a customer's video clip. Download {url} (a zip, {mb:.1f} MB), unzip it, read task.md and do "
               "exactly what it says: look at every picture yourself, then reply with only the JSON object it asks "
               "for. The link is private and stops working when you have answered.")


def bundle(content: list, schema: dict) -> tuple[Path, str]:
    """One question as a folder the Grok bot can download: the pictures and task.md in bundle.zip, under a name
    nobody can guess. Returns the folder and the zip's public address."""
    folder = settings.DIRECTOR_BUNDLE_DIR / secrets.token_urlsafe(24)
    folder.mkdir(parents=True)
    lines, n = [], 0
    with zipfile.ZipFile(folder / "bundle.zip", "w", zipfile.ZIP_STORED) as archive:   # JPEGs don't compress
        for block in content:
            if block.get("type") == "image":
                n += 1
                archive.writestr(f"{n:03d}.jpg", base64.standard_b64decode(block["source"]["data"]))
                lines.append(f"[picture {n:03d}.jpg]")
            else:
                lines.append(block["text"])
        archive.writestr("task.md", GROK_TASK.format(system=SYSTEM, n=n, last=f"{n:03d}.jpg",
                                                     body="\n\n".join(lines), schema=json.dumps(schema, indent=1)))
    return folder, f"{settings.DIRECTOR_BUNDLE_URL}/{folder.name}/bundle.zip"


class GrokDirector:
    """The Grok bot as the director, through its HTTP gateway (POST /v1/runs, then GET /v1/runs/{id} until the
    run ends). The gateway takes words only, so each question's pictures wait in a bundle the bot downloads;
    the bundle is deleted as soon as the question is settled. The bot runs on the owner's Grok plan: a call has
    no metered price, so the ledger records it at $0.

    A run that the gateway could not hand to the bot, or an answer that is not the JSON asked for, goes again.
    A run that hit the gateway's 30 minute limit does not: the same question would only time out again."""
    POLL = 10

    def __init__(self, http=None):
        if not settings.GROK_DIRECTOR_URL:
            raise RuntimeError("GROK_DIRECTOR_URL is not set")
        if http is None:
            import httpx
            http = httpx.Client(timeout=60)
        self.http = http

    def _run(self, prompt: str, step: str, sleep) -> str:
        base = settings.GROK_DIRECTOR_URL
        sent = self.http.post(f"{base}/v1/runs", json={"prompt": prompt},
                              headers={"Idempotency-Key": uuid.uuid4().hex})
        sent.raise_for_status()
        run = sent.json()["run_id"]
        state: dict = {}
        for _ in range(max(1, settings.GROK_DIRECTOR_TIMEOUT // self.POLL)):
            sleep(self.POLL)
            try:
                state = self.http.get(f"{base}/v1/runs/{run}").json()
            except Exception:                    # a dropped poll is not the run's failure; ask again
                continue
            if state.get("status") == "completed":
                return str(state.get("final_output") or "")
            if state.get("status") == "timed_out":
                raise DirectorError(f"The Grok director ran out of time on the {step} question")
            if state.get("status") == "failed":
                raise RuntimeError(str(state.get("error") or "the run failed"))
        raise DirectorError(f"The Grok director did not answer the {step} question in "
                            f"{settings.GROK_DIRECTOR_TIMEOUT} s")

    def ask(self, oid: str | None, step: str, content: list, schema: dict, sleep=time.sleep) -> dict:
        folder, url = bundle(content, schema)
        prompt = GROK_PROMPT.format(url=url, mb=(folder / "bundle.zip").stat().st_size / 1e6)
        last = None
        try:
            for attempt in range(max(1, settings.GROK_DIRECTOR_TRIES)):
                try:
                    answer = self._run(prompt, step, sleep)
                except DirectorError:
                    raise
                except Exception as exc:         # not delivered, or the gateway is unreachable for a moment
                    last = exc
                    sleep(5 * 2 ** attempt)
                    continue
                if oid:
                    db.add_cost(oid, step, "grok-bot", 0, 0.0)
                try:
                    data = _json(answer)
                except ValueError as exc:
                    last = exc
                    continue
                missing = ([k for k in schema.get("required", []) if k not in data] if isinstance(data, dict)
                           else ["a JSON object"])
                if not missing:
                    return data
                last = ValueError(f"the answer lacks {', '.join(missing)}")
        finally:
            shutil.rmtree(folder, ignore_errors=True)
        raise DirectorError(f"The Grok director gave no usable {step} answer: {last}")


def locate(dc, oid: str | None, frames: list[tuple[float, object]], dur: float, sleep=time.sleep) -> dict:
    """Where the play is over time. frames: (time, picture) of whole frames."""
    content = [text(LOCATE_PROMPT.format(n=len(frames), dur=dur))]
    for t, frame in frames:
        content += [text(f"Frame at t = {t:.2f} s:"), image(frame, settings.DIRECTOR_LOCATE_PX)]
    return ask(dc, oid, "director (locate)", content, LOCATE_SCHEMA, settings.DIRECTOR_LOCATE_EFFORT, sleep)


def plan(dc, oid: str | None, zooms: list[tuple[float, object]], wholes: list[tuple[float, object]],
         hoop_sheets: list, context: dict, dur: float, fps: float, tone: str, lang: str, sleep=time.sleep) -> dict:
    """The facts, the cast and the pages, from zoom frames that follow the play."""
    content = [text(PLAN_PROMPT.format(
        n=len(zooms), dur=dur, fps=round(fps, 2), tone=tone, lang=lang,
        whole_note=WHOLE_NOTE.format(n=len(wholes)) if wholes else "",
        hoop_note=HOOP_NOTE if hoop_sheets else "", context=json.dumps(context, ensure_ascii=False)))]
    for t, frame in wholes:
        content += [text(f"Whole frame at t = {t:.2f} s:"), image(frame, 640)]
    for t, frame in zooms:
        content += [text(f"Zoom frame at t = {t:.2f} s:"), image(frame, settings.DIRECTOR_ZOOM_PX)]
    for sheet in hoop_sheets:
        content += [text("Basket close-ups:"), image(sheet, 1600)]
    return ask(dc, oid, "director (plan)", content, PLAN_SCHEMA, None, sleep)


def review(dc, oid: str | None, step: str, entries: list[dict], sheet, facts: dict, style: str,
           sleep=time.sleep) -> dict:
    """The director's verdict and lettering for each drawing, by panel id. Each entry: {"id", "direction",
    "ref": path or None, "new": path, "old": path or None}; with "old", the director keeps the better drawing."""
    compare = any(e.get("old") for e in entries)
    content = [text(REVIEW_PROMPT.format(
        n=len(entries), compare_note=COMPARE_NOTE if compare else "",
        facts=json.dumps(facts, ensure_ascii=False), style=style,
        keep_rule=('"new" or "old" for a panel with two drawings: the one you keep; "new" for the others.'
                   if compare else '"new".')))]
    if sheet:
        content += [text("Character sheet of the lead:"), image(sheet, 768)]
    for e in entries:
        content.append(text(f"Panel {e['id']}. Your direction: {e['direction']}"))
        if e.get("ref"):
            content += [text("Reference crop:"), image(e["ref"], 512)]
        if e.get("old"):
            content += [text("First drawing (old):"), image(e["old"], 768), text("Fix (new):")]
        else:
            content.append(text("Drawing:"))
        content.append(image(e["new"], 896))
    answer = ask(dc, oid, step, content, REVIEW_SCHEMA, None, sleep)
    return {str(v.get("id")): v for v in answer.get("panels") or [] if isinstance(v, dict)}
