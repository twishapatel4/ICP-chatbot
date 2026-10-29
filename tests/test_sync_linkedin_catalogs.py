import importlib.util
import json
import shutil
from pathlib import Path

import httpx
import pytest
import respx

from icp_chatbot.engine import profile_form
from icp_chatbot.engine.profile_form import visible_cards
from icp_chatbot.linkedin.catalog import DATA_DIR, OptionCatalog

SCRIPT = Path(__file__).parent.parent / "scripts" / "sync_linkedin_catalogs.py"
_spec = importlib.util.spec_from_file_location("sync_linkedin_catalogs", SCRIPT)
sync = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync)

BASE = "https://api.linkedin.com/v2"
HR_FRESHER = {"experience_level": ["Fresher"]}


def _name(text):
    return {"localized": {"en_US": text}, "preferredLocale": {"country": "US", "language": "en"}}


def _degree(did, name, rollup, aliases=()):
    return {
        "id": did,
        "name": _name(name),
        "rollup": rollup,
        "aliases": [_name(a) for a in aliases],
    }


def _page(elements, start, total):
    return {
        "paging": {"start": start, "count": len(elements), "total": total},
        "elements": elements,
    }


@respx.mock
def test_fetch_all_follows_paging():
    route = respx.get(f"{BASE}/degrees")
    route.side_effect = [
        httpx.Response(200, json=_page([{"id": 1}, {"id": 2}], 0, 5)),
        httpx.Response(200, json=_page([{"id": 3}, {"id": 4}], 2, 5)),
        httpx.Response(200, json=_page([{"id": 5}], 4, 5)),
    ]
    with httpx.Client() as client:
        rows = sync.fetch_all("/degrees", "secret-test-token", client, count=2)
    assert [r["id"] for r in rows] == [1, 2, 3, 4, 5]
    assert route.call_count == 3
    starts = [call.request.url.params["start"] for call in route.calls]
    assert starts == ["0", "2", "4"]
    assert all(call.request.url.params["count"] == "2" for call in route.calls)
    assert all(
        call.request.headers["Authorization"] == "Bearer secret-test-token" for call in route.calls
    )


@respx.mock
def test_fetch_all_stops_on_empty_page_without_total():
    route = respx.get(f"{BASE}/fieldsOfStudy")
    route.side_effect = [
        httpx.Response(200, json={"elements": [{"id": 1}]}),
        httpx.Response(200, json={"elements": []}),
    ]
    with httpx.Client() as client:
        rows = sync.fetch_all("/fieldsOfStudy", "t", client, count=1)
    assert [r["id"] for r in rows] == [1]
    assert route.call_count == 2


@respx.mock
def test_fetch_all_raises_on_http_error():
    respx.get(f"{BASE}/degrees").mock(return_value=httpx.Response(401, json={}))
    with httpx.Client() as client, pytest.raises(httpx.HTTPStatusError):
        sync.fetch_all("/degrees", "bad", client)


def test_degree_rows_become_options_with_rollup_parent_and_aliases():
    rows = [
        _degree(258, "Bachelor of Architecture", 200, ["B.Arch", "BArch"]),
        _degree(200, "Bachelor's Degree", 200),
        _degree(7, "Doctor of Medicine", "urn:li:degree:900"),
    ]
    opts = sync.to_options("degree", rows)
    arch, bachelor, md = opts
    assert (arch.facet, arch.id, arch.label) == ("degree", "258", "Bachelor of Architecture")
    assert arch.parent == "200"
    assert arch.match == ("B.Arch", "BArch")
    assert bachelor.parent == "200"
    assert bachelor.match == ()
    assert md.parent == "900"


def test_field_of_study_options_use_first_rollup_id():
    rows = [
        {"id": 3, "name": _name("Computer Science"), "rollupIds": [10, 11]},
        {"id": 10, "name": _name("Engineering"), "rollupIds": []},
        {"id": 4, "name": "Plain Name"},
    ]
    cs, eng, plain = sync.to_options("field_of_study", rows)
    assert (cs.facet, cs.id, cs.label, cs.parent) == (
        "field_of_study",
        "3",
        "Computer Science",
        "10",
    )
    assert eng.parent is None
    assert (plain.label, plain.parent) == ("Plain Name", None)


def test_rows_without_id_or_english_name_are_skipped():
    rows = [
        {"name": _name("No id")},
        {"id": 1, "name": {"localized": {"fr_FR": "Licence"}}},
        _degree(2, "Master's Degree", 2),
    ]
    assert [o.id for o in sync.to_options("degree", rows)] == ["2"]


def test_options_serialise_in_the_catalog_file_shape():
    opts = sync.to_options("degree", [_degree(258, "Bachelor of Architecture", 200, ["B.Arch"])])
    assert sync.option_rows(opts) == [
        {"id": "258", "label": "Bachelor of Architecture", "parent": "200", "match": ["B.Arch"]}
    ]


def test_missing_token_exits_with_message(monkeypatch, tmp_path):
    monkeypatch.delenv("LINKEDIN_TOKEN", raising=False)
    with pytest.raises(SystemExit) as exc:
        sync.main(["--token-env", "LINKEDIN_TOKEN", "--out-dir", str(tmp_path)])
    assert "LINKEDIN_TOKEN" in str(exc.value.code)
    assert list(tmp_path.iterdir()) == []


def test_token_is_not_a_cli_argument(capsys):
    with pytest.raises(SystemExit) as exc:
        sync.main(["--token", "abc"])
    assert exc.value.code == 2  # argparse usage error, not an abbreviation of --token-env
    assert "unrecognized arguments" in capsys.readouterr().err


@respx.mock
def test_main_writes_both_files_from_env_token(monkeypatch, tmp_path):
    monkeypatch.setenv("LINKEDIN_TOKEN", "secret-test-token")
    degrees = respx.get(f"{BASE}/degrees").mock(
        return_value=httpx.Response(
            200, json=_page([_degree(258, "Bachelor of Architecture", 200, ["B.Arch"])], 0, 1)
        )
    )
    fields = respx.get(f"{BASE}/fieldsOfStudy").mock(
        return_value=httpx.Response(
            200,
            json=_page([{"id": 3, "name": _name("Computer Science"), "rollupIds": [10]}], 0, 1),
        )
    )
    assert sync.main(["--token-env", "LINKEDIN_TOKEN", "--out-dir", str(tmp_path)]) == 0
    assert degrees.called and fields.called
    assert json.loads((tmp_path / "degrees.json").read_text()) == [
        {"id": "258", "label": "Bachelor of Architecture", "parent": "200", "match": ["B.Arch"]}
    ]
    assert json.loads((tmp_path / "fields_of_study.json").read_text()) == [
        {"id": "3", "label": "Computer Science", "parent": "10", "match": []}
    ]
    assert "secret-test-token" not in "".join(p.read_text() for p in tmp_path.iterdir())


@respx.mock
def test_main_writes_nothing_when_a_fetch_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("LINKEDIN_TOKEN", "t")
    respx.get(f"{BASE}/degrees").mock(
        return_value=httpx.Response(200, json=_page([_degree(1, "BSc", 1)], 0, 1))
    )
    respx.get(f"{BASE}/fieldsOfStudy").mock(return_value=httpx.Response(500))
    with pytest.raises(SystemExit) as exc:
        sync.main(["--token-env", "LINKEDIN_TOKEN", "--out-dir", str(tmp_path)])
    assert exc.value.code != 0
    assert list(tmp_path.iterdir()) == []


def _data_dir(tmp_path):
    data = tmp_path / "data"
    shutil.copytree(DATA_DIR, data)
    for name in ("degrees.json", "fields_of_study.json"):
        (data / name).unlink(missing_ok=True)
    return data


def _write_synced(data):
    degrees = sync.to_options("degree", [_degree(258, "Bachelor of Architecture", 200, ["B.Arch"])])
    fields = sync.to_options("field_of_study", [{"id": 3, "name": _name("Computer Science")}])
    (data / "degrees.json").write_text(json.dumps(sync.option_rows(degrees)))
    (data / "fields_of_study.json").write_text(json.dumps(sync.option_rows(fields)))


def test_catalog_has_degree_after_files_present(tmp_path):
    data = _data_dir(tmp_path)
    assert not OptionCatalog(data).has("degree")
    _write_synced(data)
    cat = OptionCatalog(data)
    assert cat.has("degree") and cat.has("field_of_study")
    found = cat.resolve("degree", "b.arch")
    assert found is not None and found.id == "258" and found.parent == "200"
    assert cat.resolve("field_of_study", "Computer Science").id == "3"


def test_hr_education_card_available_once_both_files_exist(monkeypatch, tmp_path):
    data = _data_dir(tmp_path)
    monkeypatch.setattr(profile_form, "default_catalog", lambda: OptionCatalog(data))
    assert "education" not in [c.id for c in visible_cards("hr", HR_FRESHER)]
    _write_synced(data)
    monkeypatch.setattr(profile_form, "default_catalog", lambda: OptionCatalog(data))
    assert "education" in [c.id for c in visible_cards("hr", HR_FRESHER)]


@pytest.mark.parametrize(
    "content",
    ["", "   \n", "not json {", "{}", "null", '{"id": "1", "label": "x"}', "[]", "[1, 2]"],
)
def test_empty_or_malformed_files_leave_has_false(tmp_path, content):
    data = _data_dir(tmp_path)
    (data / "degrees.json").write_text(content)
    (data / "fields_of_study.json").write_text(content)
    cat = OptionCatalog(data)
    assert not cat.has("degree")
    assert not cat.has("field_of_study")
    assert cat.options("degree") == []
    assert cat.has("industry")  # bundled facets are unaffected


def test_rows_missing_id_or_label_make_the_file_malformed(tmp_path):
    data = _data_dir(tmp_path)
    (data / "degrees.json").write_text(json.dumps([{"id": "1", "label": "BSc"}, {"id": "2"}]))
    assert not OptionCatalog(data).has("degree")


def test_malformed_bundled_file_still_raises(tmp_path):
    data = _data_dir(tmp_path)
    (data / "industries.json").write_text("not json {")
    with pytest.raises(ValueError):
        OptionCatalog(data)
