"""The lemma note types: frozen field list, dark minimal glyph templates.

Field order is frozen (hash-guarded in tests): Anki reimport updates by field
position, so reordering silently corrupts decks. Changed fields mean a new
MODEL_ID, not an edit (docs/0003).

Design: zinc-grey base with a single darker-green accent; inline lucide-style
icons (stroke); the only pictograms are play / speed / warning.
"""

import genanki

MODEL_ID = 1717924001
COGNATE_MODEL_ID = 1717924002

FIELDS = [
    "Lemma",
    "Gloss",
    "Variants",
    "Meta",
    "Forms",
    "ExTarget",
    "ExKnown",
    "FalseFriendNote",
    "WordAudioFile",
    "ExAudioFile",
]

_ACCENT = "#15803d"

_PLAY_JS = (
    "<script>function r(w,i){var b=document.querySelectorAll("
    "'#'+w+' .replay-button, #'+w+' .replaybutton');if(b.length>i)b[i].click();}</script>"
)

_ICON_PLAY = (
    '<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">'
    '<polygon points="6 3 20 12 6 21 6 3"/></svg>'
)
_ICON_GAUGE = (
    '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="m12 14 4-4"/><path d="M3.34 19a10 10 0 1 1 17.326 0"/></svg>'
)
_ICON_WARN = (
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" '
    'style="vertical-align:-2px;margin-right:6px">'
    '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/>'
    '<path d="M12 9v4"/><path d="M12 17h.01"/></svg>'
)


def _audio_btns(field: str, aid: str) -> str:
    return (
        f'<span class="audio-hidden" id="{aid}">{{{{{field}}}}}</span>'
        f'<span class="abtn" onclick="r(\'{aid}\',0)">{_ICON_PLAY}</span>'
    )


_BACK_INNER = (
    """
<div class="w-head">
  <div class="w-lemma">{{Lemma}}</div>
  {{#Meta}}<div class="w-facets">{{Meta}}{{#Forms}} — {{Forms}}{{/Forms}}</div>{{/Meta}}
  {{#Variants}}<div class="w-facets">{{Variants}}</div>{{/Variants}}
</div>
<hr class="hair">
<div class="gloss">{{Gloss}}</div>
{{#ExTarget}}
<div class="example">
  <div class="ex-t">{{ExTarget}}</div>
  {{#ExKnown}}<div class="ex-k">{{ExKnown}}</div>{{/ExKnown}}
  {{#ExAudioFile}}<div class="audio-row">"""
    + _audio_btns("ExAudioFile", "exa")
    + """</div>{{/ExAudioFile}}
</div>
{{/ExTarget}}
{{#FalseFriendNote}}<div class="warn">"""
    + _ICON_WARN
    + """{{FalseFriendNote}}</div>{{/FalseFriendNote}}
"""
    + _PLAY_JS
    + """
"""
)

_BACK_OUTER = '<div class="outer">' + _BACK_INNER + "</div>"

_DISCOVER_Q = (
    '<div class="outer centered"><div class="tag">słowo</div>'
    '<div class="lemma">{{Lemma}}</div>'
    '<div class="audio-row">' + _audio_btns("WordAudioFile", "wa") + "</div></div>" + _PLAY_JS
)

_TYPE_Q = (
    '<div class="outer centered"><div class="tag">napisz</div>'
    '<div class="gloss">{{Gloss}}</div>'
    '{{#Meta}}<div class="muted">{{Meta}}</div>{{/Meta}}'
    '<div class="type-row">{{type:Lemma}}</div></div>'
)

_TYPE_A = (
    '<div class="outer centered"><div class="tag">napisz</div>'
    '<div class="type-row">{{type:Lemma}}</div></div>' + _BACK_OUTER
)

_CSS = (
    """
/* palette: zinc greys, one darker-green accent */
.card { background: #0e0e10; color: #e4e4e7; font-family: -apple-system, "Segoe UI", Roboto, "Helvetica Neue", sans-serif; font-size: 19px; }
.outer { max-width: 620px; margin: 0 auto; padding: 30px 22px 34px; text-align: left; }
.outer.centered { text-align: center; }

.tag { font-size: 10px; font-weight: 600; letter-spacing: 2.5px; text-transform: uppercase; color: #71717a; }

.lemma { font-size: 38px; font-weight: 650; letter-spacing: .5px; margin: 18px 0 14px; color: #f4f4f5; }
.w-head { margin-bottom: 2px; }
.w-lemma { font-size: 22px; font-weight: 600; color: #f4f4f5; }
.w-facets { font-size: 13px; color: #71717a; margin-top: 3px; }
.hair { border: 0; border-top: 1px solid #2a2c31; margin: 16px 0; }
.gloss { font-size: 28px; font-weight: 600; color: """
    + _ACCENT
    + """; margin: 12px 0; }

.audio-row { margin: 8px 0; display: flex; gap: 8px; justify-content: center; }
.example .audio-row { justify-content: flex-start; }
.abtn { display: inline-flex; align-items: center; justify-content: center; width: 36px; height: 36px; border: 1px solid #2a2c31; border-radius: 10px; color: #a1a1aa; background: #17181b; cursor: pointer; }
.abtn:hover { color: #e4e4e7; border-color: #3f3f46; }
.abtn:active { color: """
    + _ACCENT
    + """; border-color: """
    + _ACCENT
    + """; transform: scale(.94); }
.replay-button, .replaybutton { display: none !important; }
.audio-hidden { visibility: hidden; position: absolute; height: 0; width: 0; overflow: hidden; }
.audio-hidden .replay-button, .audio-hidden .replaybutton { display: inline !important; }

.example { margin-top: 22px; padding: 14px 16px; background: #131417; border-left: 2px solid #2a2c31; border-radius: 0 8px 8px 0; }
.ex-t { font-size: 19px; color: #f4f4f5; line-height: 1.45; }
.ex-k { color: #a1a1aa; font-size: 15px; margin-top: 5px; line-height: 1.4; }
.ex-k + .audio-row { margin-top: 10px; }

.muted { color: #71717a; font-size: 13px; margin-top: 12px; }
.warn { margin-top: 16px; padding: 11px 13px; background: #131417; border-left: 2px solid """
    + _ACCENT
    + """; border-radius: 0 8px 8px 0; color: #d4d4d8; font-size: 14px; }
.warn svg { color: """
    + _ACCENT
    + """; }

.type-row { margin: 18px auto 0; max-width: 420px; }
#typeans { font-size: 21px !important; background: #17181b !important; color: #e4e4e7 !important; border: 1px solid #2a2c31 !important; border-radius: 10px !important; padding: 10px !important; text-align: center !important; }
#typeans:focus { border-color: """
    + _ACCENT
    + """ !important; outline: none !important; }
.typeGood { background: rgba(21,128,61,.14) !important; color: #4ade80 !important; }
.typeBad { background: #2a1518 !important; color: #f87171 !important; }
.typeMissed { background: #131417 !important; color: #d4d4d8 !important; }
.android .lemma { font-size: 34px; }
"""
)

_TEMPLATES = [
    {"name": "Discover", "qfmt": _DISCOVER_Q, "afmt": _BACK_OUTER},
    {"name": "Type it", "qfmt": _TYPE_Q, "afmt": _TYPE_A},
]


def lemma_model() -> genanki.Model:
    """The full lemma note type (Discover + Type it)."""
    return genanki.Model(
        MODEL_ID,
        "parlance-lemma",
        fields=[{"name": f} for f in FIELDS],
        templates=_TEMPLATES,
        css=_CSS,
    )


def cognate_model() -> genanki.Model:
    """Free-cognate lemma note type: typing only, no recognition card.

    Recognition suppression is structural (a separate model), not the
    blank-front trick: Anki's required-fields rule is conjunctive on
    non-empty values and cannot express 'generate when field is empty'.
    """
    base = lemma_model()
    return genanki.Model(
        COGNATE_MODEL_ID,
        "parlance-lemma-cognate",
        fields=base.fields,
        templates=[base.templates[1]],
        css=base.css,
    )
