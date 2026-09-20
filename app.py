import csv
import io
import random
import unicodedata
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import streamlit as st

# ─────────────────────────────────────────────────────────────────────────────
# Asetukset
# ─────────────────────────────────────────────────────────────────────────────
RESULTS_FILE = Path(__file__).parent / "tulokset_log.txt"
RETRY_GAP = 3  # vähintään näin monta muuta kysymystä ennen saman lajin uusintaa

st.set_page_config(page_title="Kasvilajituntemus Quiz", page_icon="🌱")


# ─────────────────────────────────────────────────────────────────────────────
# Tietorakenne
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Plant:
    name: str
    images: tuple  # kaikki kuva-URLit tälle lajille


# ─────────────────────────────────────────────────────────────────────────────
# Tiedostojen luku
# ─────────────────────────────────────────────────────────────────────────────
def _read_text(source, encoding: str = "utf-8-sig") -> str:
    """Lukee tekstin Streamlit-uploadobjektista tai tiedostopolusta."""
    if hasattr(source, "read"):
        raw = source.read()
        return raw.decode(encoding) if isinstance(raw, bytes) else raw
    with open(source, encoding=encoding) as f:
        return f.read()


def load_plants(source) -> dict:
    """Palauttaa dict[nimi -> Plant]. Hyväksyy Streamlit-uploadobjektin tai polun.
    CSV:ssä täytyy olla sarakkeet 'species' (tai 'laji') ja 'image_url'."""
    content = _read_text(source)
    grouped = defaultdict(list)
    reader = csv.DictReader(io.StringIO(content))
    cols = {(c or "").strip().lower(): c for c in (reader.fieldnames or [])}
    name_col = cols.get("species") or cols.get("laji")
    url_col  = cols.get("image_url") or cols.get("url") or cols.get("kuva_url")
    if not name_col or not url_col:
        raise ValueError(
            f"CSV:ssä täytyy olla sarakkeet 'species' ja 'image_url'. "
            f"Löytyi: {reader.fieldnames}"
        )
    for row in reader:
        name = (row.get(name_col) or "").strip()
        url  = (row.get(url_col)  or "").strip()
        if name and url:
            grouped[name].append(url)
    if not grouped:
        raise ValueError("CSV-tiedostosta ei löytynyt yhtään laji-kuva-paria.")
    return {n: Plant(name=n, images=tuple(urls)) for n, urls in grouped.items()}


def load_done_pairs(source, plants: dict) -> set:
    """Palauttaa set[(laji, kuva_url)] — kuvat jotka on jo vastattu oikein.

    Tukee kahta lokimuotoa:
    - Uusi (tämä sovellus): sarakkeet aika;laji;kuva_url;vastaus;tulos
    - Vanha (ilman kuva_url): kaikki lajin kuvat merkitään valmiiksi.
    """
    try:
        content = _read_text(source, encoding="utf-8")
    except UnicodeDecodeError:
        content = _read_text(source, encoding="latin-1")

    lines = content.splitlines()
    if not lines:
        return set()

    reader = csv.DictReader(lines, delimiter=";")
    fieldnames = [f.strip() for f in (reader.fieldnames or [])]
    has_image_col = "kuva_url" in fieldnames

    done: set = set()
    for row in reader:
        if (row.get("tulos") or "").strip() != "oikein":
            continue
        species = (row.get("laji") or "").strip()
        if not species:
            continue
        if has_image_col:
            img = (row.get("kuva_url") or "").strip()
            if img:
                done.add((species, img))
        else:
            # Vanha muoto: merkitään kaikki tämän lajin kuvat valmiiksi
            if species in plants:
                for url in plants[species].images:
                    done.add((species, url))
    return done


# ─────────────────────────────────────────────────────────────────────────────
# Lokitus
# ─────────────────────────────────────────────────────────────────────────────
def normalise(text: str) -> str:
    """Vertailu on riippumaton isoista/pienistä kirjaimista; ä/ö ovat merkitseviä."""
    return " ".join(unicodedata.normalize("NFC", text or "").casefold().split())


def append_result(species: str, image_url: str, user_answer: str, correct: bool) -> None:
    """Kirjoittaa yhden vastausrivin lokitiedostoon (ei koskaan ylikirjoita vanhoja)."""
    is_new = not RESULTS_FILE.exists()
    with open(RESULTS_FILE, "a", encoding="utf-8") as f:
        if is_new:
            f.write("aika;laji;kuva_url;vastaus;tulos\n")
        ts     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tulos  = "oikein" if correct else "väärin"
        answer = (user_answer or "").strip() or "(tyhjä)"
        f.write(f"{ts};{species};{image_url};{answer};{tulos}\n")


# ─────────────────────────────────────────────────────────────────────────────
# Testilogiikka
# ─────────────────────────────────────────────────────────────────────────────
def start_new_run(plants: dict, done_pairs: set) -> None:
    """Alustaa uuden testikerran.

    Vaihe 1 — yksi kuva per laji:
        Jokaiselle lajille valitaan satunnaisesti yksi kuva, jota ei ole
        vielä vastattu oikein (done_pairs). Väärin vastattu kuva kysytään
        uudelleen kunnes se menee oikein.

    Vaihe 2 — loput kuvat:
        Kun kaikki lajit on käyty vaiheessa 1, jäljellä olevat kuvat
        (muut kuvat samoista lajeista) kysytään yksitellen.
    """
    # Lasketaan jokaiselle lajille saatavissa olevat kuvat
    species_avail: dict[str, list] = {}
    for name, plant in plants.items():
        avail = [u for u in plant.images if (name, u) not in done_pairs]
        if avail:
            species_avail[name] = avail

    # Vaihe 1: yksi satunnainen kuva per laji
    # Vaihe 2: kaikki loput kuvat samalle lajille
    phase1_images: dict[str, str] = {}
    phase2_tasks:  set             = set()

    for name, avail in species_avail.items():
        shuffled = avail.copy()
        random.shuffle(shuffled)
        phase1_images[name] = shuffled[0]
        for url in shuffled[1:]:
            phase2_tasks.add((name, url))

    st.session_state.update(
        plants             = plants,
        done_pairs         = set(done_pairs),
        phase              = 1 if phase1_images else 0,
        phase1_remaining   = set(phase1_images.keys()),
        phase1_images      = phase1_images,
        phase2_tasks       = phase2_tasks,
        missed             = set(),
        recent             = deque(maxlen=RETRY_GAP),
        score              = 0,
        attempts           = 0,
        total_phase1       = len(phase1_images),
        total_phase2       = len(phase2_tasks),
        phase1_done_count  = 0,
        phase2_done_count  = 0,
        feedback           = "",
        current            = None,
    )
    pick_next_question()


def pick_next_question() -> None:
    phase  = st.session_state.phase
    recent = st.session_state.recent
    last   = recent[-1] if recent else None

    if phase == 1:
        remaining = st.session_state.phase1_remaining
        if not remaining:
            # Vaihe 1 päättyi → siirry vaiheeseen 2
            if st.session_state.phase2_tasks:
                st.session_state.phase  = 2
                st.session_state.recent = deque(maxlen=RETRY_GAP)
                pick_next_question()
            else:
                st.session_state.current = None
            return
        # Valitse laji välttäen äsken kysyttyä
        pool = (
            [n for n in remaining if n not in recent]
            or [n for n in remaining if n != last]
            or list(remaining)
        )
        name = random.choice(pool)
        # Näytetään aina sama kuva tälle lajille vaiheessa 1 (myös uusinnoissa)
        st.session_state.current = (name, st.session_state.phase1_images[name])

    elif phase == 2:
        tasks = st.session_state.phase2_tasks
        if not tasks:
            st.session_state.current = None
            return
        # Vältetään saman lajin toistamista peräkkäin
        pool = (
            [(n, u) for n, u in tasks if n not in recent]
            or [(n, u) for n, u in tasks if n != last]
            or list(tasks)
        )
        st.session_state.current = random.choice(pool)

    else:
        st.session_state.current = None


def submit_answer(user_answer: str) -> None:
    if st.session_state.current is None:
        return
    name, url = st.session_state.current
    correct   = normalise(user_answer) == normalise(name)
    st.session_state.attempts += 1

    if correct:
        st.session_state.score += 1
        st.session_state.done_pairs.add((name, url))
        st.session_state.feedback = f"✅ **Oikein!** ({name})"
        if st.session_state.phase == 1:
            st.session_state.phase1_remaining.discard(name)
            st.session_state.phase1_done_count += 1
        else:
            st.session_state.phase2_tasks.discard((name, url))
            st.session_state.phase2_done_count += 1
    else:
        st.session_state.missed.add(name)
        st.session_state.feedback = (
            f"❌ **Väärin.** Oikea vastaus oli: **{name}**. Kysytään uudelleen myöhemmin."
        )
        # Väärässä vaiheessa 1: sama kuva pysyy phase1_images[name]:ssa → näytetään uudelleen.
        # Väärässä vaiheessa 2: (name, url) pysyy phase2_tasks:ssa → näytetään uudelleen.

    append_result(name, url, user_answer, correct)
    st.session_state.recent.append(name)
    pick_next_question()


# ─────────────────────────────────────────────────────────────────────────────
# UI
# ─────────────────────────────────────────────────────────────────────────────
st.title("🌱 Kasvilajituntemus Quiz")

# ── Tiedostojen latausnäkymä ──────────────────────────────────────────────────
if "plants" not in st.session_state:
    st.subheader("Lataa tiedostot")

    csv_upload = st.file_uploader(
        "Lajilista \* (CSV, sarakkeet: `species`, `image_url`)", type=["csv"]
    )
    log_upload = st.file_uploader(
        "Aiempi tulosloki — valinnainen, jatkaa siitä mihin jäit (.txt)",
        type=["txt"],
    )

    if csv_upload is None:
        st.info("Lataa CSV-tiedosto aloittaaksesi.")
        st.stop()

    if st.button("▶️ Aloita testi"):
        try:
            plants = load_plants(csv_upload)
            done_pairs: set = set()
            if log_upload is not None:
                try:
                    done_pairs = load_done_pairs(log_upload, plants)
                except Exception as log_err:
                    st.warning(f"Tuloslokia ei voitu lukea, aloitetaan alusta. Virhe: {log_err}")
            start_new_run(plants, done_pairs)
            st.rerun()
        except Exception as e:
            st.error(f"Virhe: {e}")
    st.stop()


# ── Testitila ─────────────────────────────────────────────────────────────────
p1_done  = st.session_state.phase1_done_count
p1_total = st.session_state.total_phase1
p2_done  = st.session_state.phase2_done_count
p2_total = st.session_state.total_phase2
phase    = st.session_state.phase

# Tilarivi
if phase == 1:
    phase_txt    = "**Vaihe 1 / 2**"
    progress_txt = f"**Lajit:** {p1_done} / {p1_total}"
elif phase == 2:
    phase_txt    = "**Vaihe 2 / 2** — lisäkuvat"
    progress_txt = f"**Kuvat:** {p2_done} / {p2_total}"
else:
    phase_txt    = ""
    progress_txt = ""

parts = [
    phase_txt,
    f"**Pisteet:** {st.session_state.score} / {st.session_state.attempts}",
    progress_txt,
]
if st.session_state.missed:
    parts.append(f"**Virheitä:** {len(st.session_state.missed)} lajia")
st.markdown(" &nbsp;·&nbsp; ".join(p for p in parts if p))

# ── Kysymys tai loppunäkymä ───────────────────────────────────────────────────
if st.session_state.current is not None:
    name, url = st.session_state.current
    st.image(url, use_container_width=True)

    with st.form("answer_form", clear_on_submit=True):
        answer    = st.text_input("Mikä laji on kyseessä?", key="answer_box")
        submitted = st.form_submit_button("Tarkista")
    if submitted:
        submit_answer(answer)
        st.rerun()

    if st.session_state.feedback:
        st.markdown(st.session_state.feedback)

else:
    st.markdown("---")
    if p1_total == 0:
        st.markdown("### 🎉 Kaikki lajit jo opittu!")
        st.info(
            "Tulosloki kattaa kaikki CSV:n kuvat. "
            "Lataa uudet tiedostot tai aloita alusta ilman lokia."
        )
    else:
        first_try = p1_total - len(st.session_state.missed)
        st.markdown("### 🎉 Testi valmis!")
        st.markdown(f"**Pisteet:** {st.session_state.score} / {st.session_state.attempts}")
        st.markdown(
            f"**Vaihe 1 — heti ensimmäisellä kerralla oikein:** "
            f"{first_try} / {p1_total} lajia"
        )
        if p2_total > 0:
            st.markdown(f"**Vaihe 2 — lisäkuvat käyty läpi:** {p2_total} kuvaa")
        if st.session_state.missed:
            st.markdown("**Väärin vastatut lajit:**")
            for n in sorted(st.session_state.missed):
                st.markdown(f"- {n}")

# ── Napit ─────────────────────────────────────────────────────────────────────
st.markdown("")
col1, col2 = st.columns(2)
with col1:
    if st.button("🔄 Uusi kierros (sama CSV, tyhjennä pisteet)"):
        plants_backup = st.session_state.plants
        for k in list(st.session_state.keys()):
            del st.session_state[k]
        start_new_run(plants_backup, set())
        st.rerun()
with col2:
    if st.button("📂 Lataa uudet tiedostot"):
        for k in list(st.session_state.keys()):
            del st.session_state[k]
        st.rerun()

# ── Tulosloki ─────────────────────────────────────────────────────────────────
st.divider()
st.caption(
    f"Jokainen vastaus tallennetaan tiedostoon `{RESULTS_FILE.name}` "
    "(aika;laji;kuva_url;vastaus;tulos). Vanhat tulokset säilyvät aina."
)
if RESULTS_FILE.exists():
    with open(RESULTS_FILE, "rb") as f:
        st.download_button(
            "⬇️ Lataa koko tulosloki",
            data=f.read(),
            file_name=RESULTS_FILE.name,
            mime="text/plain",
        )
    with st.expander("Näytä viimeisimmät tulokset"):
        lines = RESULTS_FILE.read_text(encoding="utf-8").splitlines()
        st.text("\n".join(lines[-20:]))
