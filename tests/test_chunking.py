from loitkb.chunking import _pack, _split_long, chunk_document, chunk_markdown, chunk_python


def test_markdown_keeps_heading_path_and_context():
    text = "# Xray UI RPA\n\nIntro text that is long enough to keep as a chunk here.\n\n## Proven wizard\n\nClick Import then From csv and pick the file to upload."
    chunks = chunk_markdown(text, "Xray UI RPA", min_chars=10)
    wizard = [c for c in chunks if c.heading.endswith("Proven wizard")][0]
    assert wizard.heading == "Xray UI RPA > Proven wizard"
    assert wizard.context == "Xray UI RPA > Proven wizard"
    assert wizard.embed_text.startswith("Xray UI RPA > Proven wizard\n")


def test_title_not_duplicated_in_context():
    chunks = chunk_markdown("# Retention\n\nLogs are kept for ninety days and then purged automatically.", "Retention", min_chars=10)
    assert chunks[0].context == "Retention"


def test_front_matter_and_code_fence_headings_ignored():
    text = "---\ndescription: x\n---\n# Doc\n\n```\n# not a heading\n```\n\nBody paragraph that is definitely long enough to survive."
    chunks = chunk_markdown(text, "Doc", min_chars=10)
    assert all("description:" not in c.text for c in chunks)
    assert all("not a heading" not in c.heading for c in chunks)


def test_no_chunk_exceeds_max_and_no_word_is_cut():
    words = [f"word{i}" for i in range(2000)]
    text = "# Big\n\n" + " ".join(words)
    chunks = chunk_markdown(text, "Big", max_chars=300, overlap=60)
    assert all(len(c.text) <= 300 for c in chunks)
    seen = set()
    for c in chunks:
        for token in c.text.split():
            assert token in words
            seen.add(token)
    assert seen == set(words)


def test_pack_overlap_never_overflows():
    units = ["a" * 140, "b" * 140, "c" * 290]
    assert all(len(p) <= 300 for p in _pack(units, 300, 150))


def test_split_long_prefers_paragraphs():
    text = ("First paragraph sentence. " * 5).strip() + "\n\n" + ("Second paragraph sentence. " * 5).strip()
    parts = _split_long(text, 200)
    assert parts[0].startswith("First") and parts[1].startswith("Second")


def test_python_splits_on_definitions():
    src = "import os\n\n\ndef alpha():\n    return 'alpha value here'\n\n\nclass Beta:\n    def run(self):\n        return 'beta value here'\n"
    names = [c.heading for c in chunk_python(src, "mod.py")]
    assert "alpha" in names and "Beta" in names


def test_dispatch_by_kind():
    assert chunk_document("plain text " * 10, "t", "text")[0].context == "t"
