"""Candidate scene-writer texts for `bench/action_prompts.py`, as overrides of `lorekeeper.art`'s constants.

Each candidate is built from the constants `art` has on this branch (so "old" is exactly today's writer) and is applied
with `patched(name)`, which sets the module's globals and puts them back after. `Illustrator` reads `SCENE_WRITER`,
`COMIC_WRITER` and `TASKS` at call time, so a patch changes the prompts it writes and nothing else. Every rule here is
general: no book, person, place or scene is named.
"""

from __future__ import annotations

import contextlib

from lorekeeper import art

OLD_FORM = "English, one paragraph, " + art._SCENE_FORM
# Since Phase B the branch's art.py carries the staging (art.STAGING, the plain-words c2): the Phase A candidates, built on
# the writer from before it, exist only on such a writer. "new" is always the branch's own text.
PHASE_A = OLD_FORM in art.SCENE_WRITER

# -- the parts the candidates share ------------------------------------------------------------------------------------

# The writer's strict look rules ("never give a specific look the material does not state") read as forbidding any pose or
# camera the book does not state: say that the staging is the writer's, within what the material tells of the moment.
_YOURS = ("- The instant, the poses, the camera and the layout of the frame are yours to choose, as an illustrator's would be: "
          "they are not a look the material lacks. They must fit what the material tells of the moment: who is there, what "
          "they do, with what, and where.\n")

_INSTANT = ("- The picture freezes one instant of the moment: choose the one that tells it best. When the moment is action (a "
            "blow, a fight, a chase, a leap, a fall, a struggle), choose the instant at its height: a blade halfway through "
            "its swing or meeting its mark, a body in the air, a grip at its hardest, never the stance before the action "
            "starts or after it ends. Where the material says the action is stopped, show the instant it is stopped. Write that "
            "instant as it happens, in the present tense: never what has just happened or is about to.\n")

_BODIES = ("- Say how each figure's body carries that instant: the line of the body (lunging, twisting, braced, thrown back, "
           "falling), where the weight is (on the front foot, off balance, in the air), what the arms and legs are doing, "
           "where the head and eyes turn, and what the motion does to what moves with it (a cloak or coat flaring, hair "
           "whipping, snow, dust, water or sparks thrown up). When the moment is quiet or full of feeling, the stillness is the "
           "point: one telling posture, gesture or gaze for each figure.\n")

_CAMERA = ("- Place the camera as a film director would for this instant: the shot (a wide shot, a full-length shot, a medium "
           "shot, a close shot), the angle (eye level, low, high, from above, over one figure's shoulder) and the layout of "
           "the frame (the action along a strong diagonal or a level, balanced frame; what is near the viewer and what "
           "behind; where the eye goes first). Low and close gives force, wide gives a chase, a crowd or a place its room, "
           "level and steady suits a quiet moment. Use only framing and angle words: no camera makes, lenses, film stocks "
           "or settings, since the same text is drawn in many styles.\n")

STAGING = _INSTANT + _BODIES + _CAMERA + _YOURS


def _scene_writer(form: str, staging: str) -> str:
    if not PHASE_A:
        return ""
    out = art.SCENE_WRITER.replace(OLD_FORM, form).replace(art.SCENE_HANDS, staging + art.SCENE_HANDS)
    assert out.count(staging) == 1 and form in out
    return out


def _comic_writer(staging: str) -> str:
    """The comic writer with the same staging rules, for each panel (its own form and its panel rules unchanged)."""
    if not PHASE_A:
        return ""
    out = art.COMIC_WRITER.replace(art.SCENE_HANDS, staging.replace("The picture freezes", "Each panel freezes")
                                   + art.SCENE_HANDS)
    assert out != art.COMIC_WRITER
    return out


# c1: the action first (Google's [Subject] + [Action] + [Location] + [Composition] order, with the action as the main clause)
C1_FORM = ("English, one paragraph, 60 to 150 words, plain sentences in the present tense, in this order: what happens at "
           "the instant the picture shows, who does what to whom, in strong verbs; the figures as the other rules allow; the "
           "setting as far as the material gives it; the camera and the layout of the frame; then the light and mood. The staging needs words: keep each look to what "
           "marks the figure, and never pass 150 words.")
# c2: the camera first (the Gemini docs' template, "A [type of shot] of [subject] in [setting]... Shot from a [camera angle]")
C2_FORM = ("English, one paragraph, 60 to 150 words, plain sentences in the present tense, in this order: the shot and the "
           "angle in a few words; then what happens at the instant the picture shows, who does what to whom, in strong verbs; "
           "the figures as the other rules allow; the setting as far as the material gives it; then the light and mood. The "
           "staging needs words: keep each look to what marks the figure, and never pass 150 words.")

TASK_EVENT = ("Write the prompt for one picture of the moment: one instant of it, what happens, who is in frame, the camera, "
              "the layout of the frame and the light.")
TASK_PLATE = ("Write the prompt for one picture of the main moment of this chapter: one instant of it, what happens, who is "
              "in frame, the camera, the layout of the frame and the light.")


def _tasks() -> dict:
    return {**art.TASKS, "event": TASK_EVENT, "plate": TASK_PLATE}


# c3: c2 plus a readable action (O1: "framing, scale, gaze, and object interaction") and the moment kept whole (round 1 lost
# fidelity where the writer chose a more forceful instant than the one the material tells)
_READABLE = ("- Keep the action readable at a glance: the one who acts and the one acted on both in clear view, the hands and "
             "what they hold or strike with unhidden, the action large in the frame, faces turned so their expression shows.\n")
_WHOLE = ("- Everyone and everything the moment names stays in the picture, doing what the material says they do; the instant "
          "you choose is one the material tells, not one you add.\n")
STAGING3 = _INSTANT + _BODIES + _CAMERA + _READABLE + _YOURS + _WHOLE

# c3L: c3 plus a real-world lookup in the material (`<real_world>`, written by the bench's lookup step, never by the book)
_REAL_LINE = ("<real_world>: how real-world things in the moment (a weapon, armour, dress, a building, a vehicle, a way of "
              "fighting or moving) look and are used, from a web search. Use it for how they look and how bodies move with "
              "them; it adds nothing to the moment, and where it disagrees with the material, the material wins.\n")


def _with_real(writer: str) -> str:
    if not PHASE_A:
        return ""
    anchor = "<book>:"
    assert writer.count(anchor) == 1
    return writer.replace(anchor, _REAL_LINE + anchor)


CANDIDATES: dict[str, dict] = {"new": {}} if not PHASE_A else {
    "new": {},
    "old": {},
    "c1": {"SCENE_WRITER": _scene_writer(C1_FORM, STAGING), "TASKS": _tasks(), "COMIC_WRITER": _comic_writer(STAGING)},
    "c2": {"SCENE_WRITER": _scene_writer(C2_FORM, STAGING), "TASKS": _tasks(), "COMIC_WRITER": _comic_writer(STAGING)},
    "c3": {"SCENE_WRITER": _scene_writer(C2_FORM, STAGING3), "TASKS": _tasks(), "COMIC_WRITER": _comic_writer(STAGING3)},
    "c3L": {"SCENE_WRITER": _with_real(_scene_writer(C2_FORM, STAGING3)), "TASKS": _tasks(),
            "COMIC_WRITER": _comic_writer(STAGING3)},
}
LOOKUP = {"c3L"}     # the arms whose prompts are written with the real-world lookup


@contextlib.contextmanager
def patched(name: str):
    """`art`'s constants as candidate `name` has them, for the duration of the block."""
    over = CANDIDATES[name]
    saved = {k: getattr(art, k) for k in over}
    try:
        for k, v in over.items():
            setattr(art, k, v)
        yield
    finally:
        for k, v in saved.items():
            setattr(art, k, v)


def describe(name: str) -> dict:
    """What a candidate changes, for the bench's summary."""
    if name == "new":
        return {"SCENE_WRITER": "art.py on this branch", "PROMPT_VERSION": art.PROMPT_VERSION}
    return {k: (v if isinstance(v, str) else {t: v[t] for t in ("event", "plate")}) for k, v in CANDIDATES[name].items()
            if k != "COMIC_WRITER"}
