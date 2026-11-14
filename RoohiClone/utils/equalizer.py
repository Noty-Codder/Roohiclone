"""🎚 Equalizer / track-mode / volume engine (OP edition).

Every preset is a plain ffmpeg audio-filter chain (no spaces!) that is handed
to py-tgcalls through MediaStream(ffmpeg_parameters="--audio ---mid -af <chain>").
State is per chat and resets when the chat's stream ends.
"""

# Every chain ends with a soft limiter so boosted presets never clip / crackle.
# Sample-rate safe: pitch presets resample to 48 kHz first, otherwise asetrate
# shifts the pitch wrongly on 44.1 kHz sources.
_LIM = "alimiter=limit=0.95:level=0"

# key: (button name, ffmpeg -af chain)   -- no spaces inside a chain!
EQ_PRESETS = {
    "off":       ("Normal",           ""),
    # ---- bass family
    "bass":      ("Bass Boost",       f"bass=g=10:f=90:w=0.6,{_LIM}"),
    "superbass": ("Super Bass OP",    f"bass=g=16:f=60:w=0.7,equalizer=f=110:t=q:w=1:g=5,acompressor=threshold=0.25:ratio=3,{_LIM}"),
    "deep":      ("Deep Bass",        f"bass=g=15:f=55:w=0.7,lowpass=f=12000,{_LIM}"),
    "dj":        ("DJ Bass",          f"bass=g=12:f=70:w=0.6,treble=g=4,acompressor=ratio=3,dynaudnorm=f=150,{_LIM}"),
    # ---- full-power mixes
    "op":        ("OP Ultimate",      f"bass=g=14:f=70:w=0.6,equalizer=f=1500:t=q:w=1:g=2,treble=g=5:f=6000,extrastereo=m=1.6,acompressor=threshold=0.18:ratio=4:attack=5:release=80:makeup=2,{_LIM}"),
    "party":     ("Party",            f"bass=g=8,treble=g=6,extrastereo=m=2.0,dynaudnorm=f=150,{_LIM}"),
    "club":      ("Club",             f"bass=g=9:f=80:w=0.6,equalizer=f=3000:t=q:w=1:g=3,extrastereo=m=1.5,acompressor=ratio=3,{_LIM}"),
    # ---- space / 3D
    "8d":        ("8D Audio",         f"apulsator=mode=sine:hz=0.125,stereotools=mlev=0.05,bass=g=4,{_LIM}"),
    "concert":   ("Concert Hall",     f"aecho=0.8:0.88:40|70|100:0.35|0.25|0.2,bass=g=3,{_LIM}"),
    "echo":      ("Echo",             f"aecho=0.8:0.9:400:0.35,{_LIM}"),
    # ---- clarity
    "clear":     ("HD Clear",         f"highpass=f=60,equalizer=f=1000:t=q:w=1:g=3,equalizer=f=3500:t=q:w=1:g=4,dynaudnorm=f=200,{_LIM}"),
    "vocal":     ("Vocal Boost",      f"highpass=f=120,equalizer=f=2500:t=q:w=2:g=6,dynaudnorm=f=200,{_LIM}"),
    "treble":    ("Treble Boost",     f"treble=g=10:f=4000,{_LIM}"),
    "soft":      ("Soft",             "volume=0.8,lowpass=f=8000"),
    # ---- fun
    "lofi":      ("Lo-Fi",            f"highpass=f=200,lowpass=f=3500,acompressor=ratio=4,aecho=0.8:0.85:60:0.25,{_LIM}"),
    "nightcore": ("Nightcore",        "aresample=48000,asetrate=60000,aresample=48000"),
    "slowed":    ("Slowed+Reverb",    f"aresample=48000,asetrate=40800,aresample=48000,aecho=0.8:0.88:60:0.4,{_LIM}"),
    "karaoke":   ("Karaoke",          f"aformat=channel_layouts=stereo,pan=stereo|c0=c0-c1|c1=c1-c0,volume=1.6,{_LIM}"),
}

# "Track mode" button cycles through these (first one = back to normal)
TRACK_MODES = ["off", "echo", "bass", "superbass", "op", "8d", "concert", "nightcore", "slowed", "lofi", "party"]

EQ_STATE = {}        # chat_id -> preset key
VOLUME_STATE = {}    # chat_id -> volume %
MIN_VOLUME, DEFAULT_VOLUME, MAX_VOLUME = 0, 50, 1000


def _cid(chat_id):
    try:
        return int(chat_id)
    except Exception:
        return chat_id


def get_eq_filter(preset):
    data = EQ_PRESETS.get(str(preset))
    return data[1] if data else ""


def get_eq_name(preset):
    data = EQ_PRESETS.get(str(preset))
    return data[0] if data else EQ_PRESETS["off"][0]


def get_chat_eq(chat_id):
    return EQ_STATE.get(_cid(chat_id), "off")


def get_chat_eq_filter(chat_id):
    return get_eq_filter(get_chat_eq(chat_id))


def set_chat_eq(chat_id, preset):
    if str(preset) == "off":
        EQ_STATE.pop(_cid(chat_id), None)
    else:
        EQ_STATE[_cid(chat_id)] = str(preset)


def next_track_mode(chat_id):
    cur = get_chat_eq(chat_id)
    if cur not in TRACK_MODES:
        return TRACK_MODES[0]
    return TRACK_MODES[(TRACK_MODES.index(cur) + 1) % len(TRACK_MODES)]


def get_chat_volume(chat_id):
    try:
        return max(MIN_VOLUME, min(MAX_VOLUME, int(VOLUME_STATE.get(_cid(chat_id), DEFAULT_VOLUME))))
    except (TypeError, ValueError):
        return DEFAULT_VOLUME


def set_chat_volume(chat_id, volume):
    try:
        volume = int(volume)
    except (TypeError, ValueError):
        volume = DEFAULT_VOLUME
    VOLUME_STATE[_cid(chat_id)] = max(MIN_VOLUME, min(MAX_VOLUME, volume))


def reset_chat_audio(chat_id):
    """Normal EQ + default 50 % volume."""
    set_chat_eq(chat_id, "off")
    set_chat_volume(chat_id, DEFAULT_VOLUME)


def clear_chat_audio(chat_id):
    EQ_STATE.pop(_cid(chat_id), None)
    VOLUME_STATE.pop(_cid(chat_id), None)


def get_chat_volume_filter(chat_id):
    volume = get_chat_volume(chat_id)
    if volume == DEFAULT_VOLUME:
        return ""
    flt = f"volume={volume / DEFAULT_VOLUME:.2f}"
    if volume > 100:                       # boosted -> keep it clean, no clipping
        flt += f",{_LIM}"
    return flt


# ------------------------------------------------------------------ keyboards
def equalizer_markup(_, chat_id):
    from RoohiClone.utils.roohi import btn
    current = get_chat_eq(chat_id)
    rows, row = [], []
    for key, (name, _f) in EQ_PRESETS.items():
        label = f"✅ {name}" if key == current else name
        row.append(btn(label, cb=f"ADMIN EqSet:{key}|{chat_id}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([btn(f"🔊 Volume · {get_chat_volume(chat_id)}%", cb=f"ADMIN VolPanel|{chat_id}")])
    rows.append([btn("♻️ Reset", cb=f"ADMIN AudioReset|{chat_id}"), btn("Close", cb="close")])
    return rows


def volume_markup(_, chat_id):
    from RoohiClone.utils.roohi import btn
    volume = get_chat_volume(chat_id)
    return [
        [btn("Vol +50", cb=f"ADMIN VolSet:up|{chat_id}"), btn("Vol -25", cb=f"ADMIN VolSet:down|{chat_id}")],
        [btn(f"Boost +500 · {volume}%", cb=f"ADMIN VolSet:boost|{chat_id}")],
        [btn("Reset · Normal EQ · 50%", cb=f"ADMIN AudioReset|{chat_id}")],
        [btn("Back to EQ", cb=f"ADMIN VolBack|{chat_id}")],
        [btn("Close", cb="close")],
    ]
