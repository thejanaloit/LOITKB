from loitkb.answer import ABSTAIN_TEXT, _extractive, _sentences, validate_citations

HITS = [
    {"text": "Xray manual steps are imported with the CSV wizard using Action, Data and Expected Result columns."},
    {"text": "- [x] Book1 must embed a screenshot in every data row.\n- [ ] Optional extra notes are allowed."},
]


def test_valid_citations_pass():
    answer = "Xray manual steps are imported with the CSV wizard. [1]"
    assert validate_citations(answer, HITS)["valid"]


def test_missing_and_out_of_range_citations_fail():
    assert not validate_citations("Steps are imported with the wizard.", HITS)["valid"]
    bad = validate_citations("Steps are imported with the wizard. [7]", HITS)
    assert any("does not exist" in p["problem"] for p in bad["problems"])


def test_unsupported_claim_fails():
    out = validate_citations("The moon is made of green cheese and served on Tuesdays. [1]", HITS)
    assert not out["valid"] and "weak support" in out["problems"][0]["problem"]


def test_abstain_is_valid():
    assert validate_citations(ABSTAIN_TEXT, HITS)["abstained"]


def test_checkbox_markers_do_not_become_sentences():
    assert all(not s.startswith("[") for s in _sentences(HITS[1]["text"]))
    answer = _extractive("Which Book1 rows need a screenshot?", HITS)
    assert validate_citations(answer, HITS)["valid"], answer
