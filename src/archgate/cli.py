from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

import yaml

from archgate import audit, benchmark, promote, sampling
from archgate.errors import ArchGateError, RequirementsError
from archgate.knowledge import load_knowledge_base
from archgate.llm import DEFAULT_MODEL, DEFAULT_OLLAMA_URL, OllamaClient
from archgate.pipeline import Budget, Pipeline, Run, State
from archgate.requirements import Requirements
from archgate.store import load_run, save_run

EXIT_OK, EXIT_ERROR, EXIT_BLOCKED = 0, 1, 2


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return args.handler(args)
    except (ArchGateError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("\naborted", file=sys.stderr)
        return EXIT_ERROR


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="archgate",
        description="Requirements in, reviewed system design out.",
    )
    parser.add_argument(
        "--learned-rules",
        type=Path,
        default=Path(os.environ.get("ARCHGATE_LEARNED_RULES", "learned_rules.yaml")),
        help="extra rules promoted from approved runs (default: ./learned_rules.yaml)",
    )
    sub = parser.add_subparsers(required=True, metavar="command")

    design = sub.add_parser("design", help="generate a design and stop at the approval gate")
    source = design.add_mutually_exclusive_group(required=True)
    source.add_argument("requirements", nargs="?", type=Path, help="requirements YAML file")
    source.add_argument("--text", help="plain-English description (needs a model)")
    design.add_argument("--no-llm", action="store_true", help="rules only, no model")
    _add_model_args(design, default_model=os.environ.get("ARCHGATE_MODEL", DEFAULT_MODEL))
    design.add_argument("--max-llm-calls", type=int, default=6)
    design.add_argument("--max-repair-rounds", type=int, default=3)
    design.add_argument("--out", type=Path, default=Path("runs"))
    design.add_argument("--yes", action="store_true",
                        help="accept extracted requirements without asking")
    design.set_defaults(handler=_cmd_design)

    show = sub.add_parser("show", help="print a saved design document")
    show.add_argument("run_dir", type=Path)
    show.set_defaults(handler=_cmd_show)

    for name, approve in (("approve", True), ("reject", False)):
        cmd = sub.add_parser(name, help=f"{name} a design waiting at the gate")
        cmd.add_argument("run_dir", type=Path)
        cmd.add_argument("--by", default=getpass.getuser())
        cmd.add_argument("--note", default="")
        if approve:
            cmd.add_argument("--force", action="store_true",
                             help="approve despite open validation failures")
        cmd.set_defaults(handler=_cmd_decide, approve=approve)

    check = sub.add_parser("check-kb", help="validate the rule base and component catalog")
    check.add_argument("--rules", type=Path)
    check.add_argument("--components", type=Path)
    check.set_defaults(handler=_cmd_check_kb)

    prom = sub.add_parser("promote", help="turn approved model suggestions into draft rules")
    prom.add_argument("run_dir", type=Path)
    prom.add_argument("--yes", action="store_true", help="save without asking")
    prom.set_defaults(handler=_cmd_promote)

    serve = sub.add_parser("serve", help="run the rules-only web UI locally")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--database-url", default=os.environ.get("DATABASE_URL",
                                                                 "sqlite:///archgate.db"))
    serve.set_defaults(handler=_cmd_serve)

    aud = sub.add_parser("audit-rules", help="find dead, duplicate and contested rules")
    aud.add_argument("--samples", type=int, default=1000)
    aud.add_argument("--seed", type=int, default=7)
    aud.add_argument("--cases", type=Path, default=Path("benchmarks/cases"),
                     help="also include benchmark cases if the folder exists")
    aud.set_defaults(handler=_cmd_audit)

    bench = sub.add_parser("bench", help="score the rule base against reference designs")
    bench.add_argument("--cases", type=Path, default=Path("benchmarks/cases"))
    _add_model_args(bench, default_model=None)
    bench.add_argument("--json", type=Path, help="also write the full report as JSON")
    bench.add_argument("--verbose", action="store_true", help="list extra components too")
    bench.add_argument("--min-pass-rate", type=float, default=0.0,
                       help="exit non-zero below this pass rate (0-1), for CI")
    bench.set_defaults(handler=_cmd_bench)
    return parser


def _add_model_args(cmd: argparse.ArgumentParser, default_model: str | None) -> None:
    cmd.add_argument("--model", default=default_model,
                     help="Ollama model name" + ("" if default_model else " (default: rules only)"))
    cmd.add_argument("--ollama-url",
                     default=os.environ.get("ARCHGATE_OLLAMA_URL", DEFAULT_OLLAMA_URL))


def _kb(args: argparse.Namespace, **kwargs):
    return load_knowledge_base(learned_path=args.learned_rules, **kwargs)


def _cmd_design(args: argparse.Namespace) -> int:
    kb = _kb(args)
    llm = None if args.no_llm else _connect_model(args)
    if args.text and llm is None:
        raise ArchGateError("--text needs a model; start Ollama or pass a requirements file")

    source = args.text if args.text else _load_requirements(args.requirements)
    confirm = None if args.yes or not sys.stdin.isatty() else _confirm_requirements
    pipeline = Pipeline(
        kb,
        llm=llm,
        budget_factory=lambda: Budget(max_llm_calls=args.max_llm_calls,
                                      max_repair_rounds=args.max_repair_rounds),
        confirm_requirements=confirm,
    )
    run = pipeline.run(source)
    run_dir = save_run(run, args.out, kb.catalog)

    if run.state is State.FAILED:
        print(f"run failed: {run.error}", file=sys.stderr)
        return EXIT_ERROR

    _print_summary(run, run_dir)
    if sys.stdin.isatty():
        _interactive_gate(run, run_dir, kb.catalog)
    return EXIT_BLOCKED if run.confidence == "blocked" else EXIT_OK


def _cmd_show(args: argparse.Namespace) -> int:
    doc = args.run_dir / "design.md"
    if not doc.exists():
        raise ArchGateError(f"no design document in {args.run_dir}")
    print(doc.read_text())
    return EXIT_OK


def _cmd_decide(args: argparse.Namespace) -> int:
    kb = _kb(args)
    run = load_run(args.run_dir)
    run.decide(args.approve, by=args.by, note=args.note, force=getattr(args, "force", False))
    save_run(run, args.run_dir.parent, kb.catalog)
    print(f"{run.run_id}: {run.state.value} by {args.by}")
    return EXIT_OK


def _cmd_check_kb(args: argparse.Namespace) -> int:
    kb = _kb(args, components_path=args.components, rules_path=args.rules)
    learned = sum(1 for r in kb.rules if r.origin == "learned")
    print(f"ok: {len(kb.catalog.components)} components, {len(kb.catalog.settings)} settings, "
          f"{len(kb.rules)} rules ({learned} learned), version {kb.version}")
    return EXIT_OK


def _cmd_promote(args: argparse.Namespace) -> int:
    kb = _kb(args)
    drafts = promote.draft_rules(load_run(args.run_dir), kb)
    if not drafts:
        print("nothing to promote: no approved model suggestions for uncovered features")
        return EXIT_OK

    print("Draft rule(s):\n")
    print(promote.to_yaml(drafts))
    if not args.yes:
        if not sys.stdin.isatty():
            print("not saved; rerun with --yes to accept")
            return EXIT_OK
        if input(f"Add to {args.learned_rules}? [y/N]: ").strip().lower() != "y":
            print("not saved")
            return EXIT_OK
    promote.append_rules(args.learned_rules, drafts)
    print(f"saved to {args.learned_rules}. future runs will apply it without the model.")
    return EXIT_OK


def _cmd_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn

        from archgate.web.app import WebConfig, create_app
    except ImportError as exc:
        raise ArchGateError('the web UI needs extra packages: uv sync --extra web '
                            '(or pip install "archgate[web]")') from exc
    learned = args.learned_rules if args.learned_rules.exists() else None
    app = create_app(WebConfig(database_url=args.database_url, learned_rules=learned))
    print(f"ArchGate web UI on http://{args.host}:{args.port}  (rules only)")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return EXIT_OK


def _cmd_audit(args: argparse.Namespace) -> int:
    kb = _kb(args)
    reqs = sampling.samples(args.samples, seed=args.seed)
    if args.cases.is_dir():
        reqs += [case.requirements for case in benchmark.load_cases(args.cases, kb.catalog)]
    print(f"knowledge base {kb.version} ({len(kb.rules)} rules)")
    print(audit.audit(kb, reqs).format())
    return EXIT_OK


def _cmd_bench(args: argparse.Namespace) -> int:
    kb = _kb(args)
    llm = _connect_model(args) if args.model else None
    if args.model and llm is None:
        raise ArchGateError(f"model {args.model} is not available")
    cases = benchmark.load_cases(args.cases, kb.catalog)
    report = benchmark.run_benchmark(cases, Pipeline(kb, llm=llm))

    mode = f"model {llm.name}" if llm else "rules only"
    print(f"knowledge base {kb.version} ({len(kb.rules)} rules), {mode}\n")
    print(benchmark.format_report(report, verbose=args.verbose))
    if args.json:
        args.json.write_text(json.dumps(report.to_dict(), indent=2) + "\n")
    return EXIT_OK if report.pass_rate >= args.min_pass_rate else EXIT_BLOCKED


def _connect_model(args: argparse.Namespace) -> OllamaClient | None:
    client = OllamaClient(model=args.model, base_url=args.ollama_url)
    if client.is_available():
        return client
    print(
        f"note: model {args.model} not available at {args.ollama_url}; running rules only.\n"
        f"      to enable it: ollama pull {args.model}",
        file=sys.stderr,
    )
    return None


def _load_requirements(path: Path) -> Requirements:
    try:
        data = yaml.safe_load(path.read_text())
    except FileNotFoundError as exc:
        raise ArchGateError(f"file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ArchGateError(f"{path}: invalid YAML: {exc}") from exc
    return Requirements.from_dict(data)


def _confirm_requirements(req: Requirements) -> Requirements | None:
    print("\nThe model read your description as:\n")
    print(yaml.safe_dump(req.to_dict(), sort_keys=False))
    answer = input("Use these requirements? [Y]es / [e]dit / [n]o: ").strip().lower()
    if answer in ("", "y", "yes"):
        return req
    if answer in ("e", "edit"):
        return _edit_requirements(req)
    return None


def _edit_requirements(req: Requirements) -> Requirements | None:
    import subprocess
    import tempfile

    editor = os.environ.get("EDITOR", "vi")
    with tempfile.NamedTemporaryFile("w+", suffix=".yaml", delete=False) as fh:
        yaml.safe_dump(req.to_dict(), fh, sort_keys=False)
        path = Path(fh.name)
    try:
        subprocess.run([editor, str(path)], check=True)
        return Requirements.from_dict(yaml.safe_load(path.read_text()))
    except RequirementsError as exc:
        print(f"edited requirements are invalid: {exc}", file=sys.stderr)
        return None
    finally:
        path.unlink(missing_ok=True)


def _print_summary(run: Run, run_dir: Path) -> None:
    deterministic = sum(1 for c in run.design.choices.values() if c.deterministic)
    suggested = len(run.design.choices) - deterministic
    print(f"\n{run.requirements.name}: {len(run.design.choices)} components "
          f"({deterministic} from rules, {suggested} suggested by the model)")
    print(f"confidence: {run.confidence}   llm calls: {run.llm_calls}")
    for finding in run.findings:
        print(f"  FAIL {finding.check}: {finding.message}")
    for item in run.unresolved:
        print(f"  REVIEW {item}")
    suggestions = [
        (cid, reason.why)
        for cid, choice in run.design.choices.items()
        for reason in choice.reasons
        if reason.source == "llm"
    ]
    if suggestions:
        print("\nmodel suggestions (check these before approving):")
        for cid, why in suggestions:
            print(f"  + {cid}: {why}")
    print(f"\ndesign doc: {run_dir / 'design.md'}")
    print(f"trace:      {run_dir / 'trace.jsonl'}")


def _interactive_gate(run: Run, run_dir: Path, catalog) -> None:
    answer = input("\nApprove this design? [a]pprove / [r]eject / [l]ater: ").strip().lower()
    if answer not in ("a", "approve", "r", "reject"):
        print(f"left at the gate. decide later with: archgate approve {run_dir}")
        return
    approve = answer.startswith("a")
    force = False
    if approve and run.confidence == "blocked":
        force = input("validation failures are open. approve anyway? [y/N]: ").lower() == "y"
        if not force:
            print("left at the gate.")
            return
    if approve:
        note = input("note (optional, press Enter to skip): ").strip()
    else:
        note = ""
        while not note:
            note = input("reason for rejecting (required): ").strip()
    run.decide(approve, by=getpass.getuser(), note=note, force=force)
    save_run(run, run_dir.parent, catalog)
    print(f"{run.state.value}.")


if __name__ == "__main__":
    sys.exit(main())
