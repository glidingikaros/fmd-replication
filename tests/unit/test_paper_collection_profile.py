from types import SimpleNamespace

import pytest

from fmd import main, profiles
from fmd.collection import analysis, paper_host
from fmd.collection.tools.host.parser_appliance import parser_runtime
from fmd.core.hashing import sha256_file
from fmd.core.sealed_records import read_json


def resolve_once(monkeypatch):
    profile = profiles.resolve_paper_profile()
    calls = []

    def resolve():
        calls.append(profile)
        assert len(calls) == 1, "G2 must be resolved once at the entry point"
        return profile

    monkeypatch.setattr(profiles, "resolve_paper_profile", resolve)
    return profile, calls


@pytest.mark.parametrize("supplement", [False, True])
def test_public_collect_command_passes_g2_through_native_collection_callers(
    tmp_path, monkeypatch, supplement
):
    from fmd.analysis import population_binding
    from fmd.collection import alignment, factual_challenge

    generation = tmp_path / "generation"
    generation.mkdir()
    evidence = generation / "full_scale.vmdk"
    evidence.write_bytes(b"public image bytes")
    (generation / "ground_truth.json").write_text('{"private": true}')
    output = tmp_path / "collection"
    profile, resolutions = resolve_once(monkeypatch)
    public = {"members": [{"path": "a.bmp"}, {"path": "b.BMP"}, {"path": "c.txt"}]} if supplement else None
    generated = SimpleNamespace(
        content_subject_limit=22, i30_directory_paths=("directory",),
        evidence_sha256=sha256_file(evidence), population_manifest={"public": "roster"},
    )
    collected = {"parser_runs": [], "candidate_populations": []}
    calls = []

    def population(path, *, verify_evidence_sha256):
        assert path == evidence
        assert verify_evidence_sha256 is False
        return generated

    def collect_host(**kwargs):
        calls.append("host")
        assert kwargs["profile"] is profile
        assert kwargs["evidence"] == evidence
        assert kwargs["output"] == output
        assert kwargs["run_id"] == output.name
        assert kwargs["expected_sha256"] == generated.evidence_sha256
        assert kwargs["bounded_content_subject_limit"] == 22 + (2 if supplement else 0)
        assert kwargs["bounded_i30_directory_paths"] == generated.i30_directory_paths
        assert kwargs["windows_parsers"] == tmp_path / "windows-parsers"
        assert kwargs["host_toolchain_root"] == tmp_path / "toolchain"
        return collected

    def transform(name):
        def apply(index, *args, **kwargs):
            calls.append(name)
            assert index is collected
            return index

        return apply

    monkeypatch.setattr(population_binding, "load_generated_population_bundle", population)
    monkeypatch.setattr(factual_challenge, "load_public_population", lambda path: public)
    monkeypatch.setattr(paper_host, "collect_host", collect_host)
    monkeypatch.setattr(alignment, "add_native_population_surfaces", transform("native"))
    monkeypatch.setattr(population_binding, "bind_population_manifest", transform("bind"))
    monkeypatch.setattr(analysis, "add_reference_scoped_usn", transform("usn"))
    monkeypatch.setattr(analysis, "add_reference_scoped_ads", transform("ads"))
    monkeypatch.setattr(factual_challenge, "add_factual_population", transform("supplement"))
    assert main([
        "paper", "collect", "--evidence", str(evidence), "--output", str(output),
        "--windows-parsers", str(tmp_path / "windows-parsers"),
        "--host-toolchain-root", str(tmp_path / "toolchain"),
    ]) == 0
    assert resolutions == [profile]
    assert calls == ["host", "native", "bind", "usn", "ads"] + (["supplement"] if supplement else [])
    assert read_json(output / "evidence_index.json") == collected
    factual = read_json(output / "factual-collection.json")
    assert factual["status"] == "completed" and factual["truth_sources_used"] == []
    assert factual["evidence_index_sha256"] == sha256_file(output / "evidence_index.json")
    timing = read_json(output.with_name(output.name + "-timing.json"))
    guard = read_json(output.with_name(output.name + "-truth-guard.json"))
    assert timing["status"] == guard["status"] == "completed"
    assert guard["generation_files_opened"] == guard["denied_private_reads"] == []


@pytest.mark.parametrize("available", [False, True])
def test_public_preflight_command_resolves_g2_once_and_checks_its_exact_selection(
    tmp_path, monkeypatch, available
):
    profile, resolutions = resolve_once(monkeypatch)
    checked = []
    declaration = profile["collection_declaration"]
    parsers = tmp_path / "windows-parsers"
    toolchain = tmp_path / "toolchain"

    def backend(args):
        assert args.evidence is None
        assert args.windows_parsers == str(parsers)
        assert args.host_toolchain_root == toolchain
        assert args.expected_definitions_sha256 == declaration["kape_definitions"]["tree_sha256"]
        assert (args.collection_provider, args.windows_box) == parser_runtime()
        checked.append("backend")
        return {"available": available, "missing": [] if available else ["fixture"]}

    monkeypatch.setattr(paper_host, "preflight_host_collector_backend", backend)
    assert main([
        "paper", "preflight", "--windows-parsers", str(parsers),
        "--host-toolchain-root", str(toolchain),
    ]) == (0 if available else 1)
    assert resolutions == [profile]
    assert checked == ["backend"]
