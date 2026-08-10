"""``python -m ui_servo.cli.servo`` — run one gauntlet round.

This is where the round's ports become concrete: a JSONL evidence store, the nh3
sanitiser, a Playwright sensor, a preview server, and the three-family CLI panel.
:func:`ui_servo.control.servo.run_round` receives them and never learns which is
which, which is what lets the same loop run against fakes in the test suite.
"""

import argparse
import contextlib
import threading
import time
from collections.abc import Iterator, Sequence
from pathlib import Path

import uvicorn

from ui_servo.adapters import cli_judges, jsonl_store, playwright_sensor, preview_server
from ui_servo.adapters.nh3_sanitizer import default_sanitizer
from ui_servo.control.servo import (
    Stores,
    default_contract_path,
    dry_panel,
    load_anti_corpus,
    load_candidates,
    run_round,
)
from ui_servo.domain.contract import DirectionContract
from ui_servo.domain.evidence import TurnId
from ui_servo.ports.store import EvidenceStorePort


@contextlib.contextmanager
def previews(
    candidates_dir: Path,
    store: EvidenceStorePort,
    contract: DirectionContract,
    turn_id: TurnId,
) -> Iterator[str]:
    """A preview server on an ephemeral port, for the length of one round.

    Served with ``dev=False``: the probe is the instrument, and a screenshot the
    panel is going to argue about must be of the fragment rather than of the
    instrument watching it. The probe's observations reach this round the other
    way, out of the evidence stock.
    """
    app = preview_server.create_app(candidates_dir, store, contract, turn_id, dev=False)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30.0
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.02)
    if not server.started:
        raise RuntimeError("the preview server did not start")
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ui_servo.cli.servo",
        description="Run one gauntlet round: gates, blind panel, archive, report.",
    )
    parser.add_argument("--candidates", type=Path, required=True, help="directory of fragments")
    parser.add_argument("--part", required=True, help="which part of the site this round is about")
    parser.add_argument("--round", dest="round_id", required=True, help="round id, e.g. 1")
    parser.add_argument("--out", type=Path, required=True, help="where evidence and artefacts go")
    parser.add_argument("--contract", type=Path, default=None, help="direction contract TOML")
    parser.add_argument("--part-spec", type=Path, default=None, help="file holding the part brief")
    parser.add_argument(
        "--anti-corpus",
        type=Path,
        default=None,
        help="directory of generic_*.json style samples to measure blandness against",
    )
    parser.add_argument(
        "--dry-judges",
        action="store_true",
        help="replace the model panel with deterministic stubs (no model calls)",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=1,
        help="threads for the gate pass; keep at 1 with the Playwright sensor",
    )
    return parser


def build_stores(out_dir: Path) -> Stores:
    """Construct the round's two JSONL stocks under a single output directory.

    ``JsonlExemplarStore`` appends ``"exemplars/"`` to its root itself (see its
    ``__init__``); handing it ``out_dir / "exemplars"`` here used to double the
    segment, stranding exemplars at ``<out>/exemplars/exemplars/<name>/...``
    (batch, 2026-08-09).
    """
    return Stores(
        evidence=jsonl_store.JsonlEvidenceStore(out_dir),
        exemplar=jsonl_store.JsonlExemplarStore(out_dir),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    contract_path = args.contract or default_contract_path()
    if contract_path is None or not contract_path.is_file():
        parser.error("no direction contract found; set UI_SERVO_CONTRACT or pass --contract PATH")
    contract = DirectionContract.from_toml(contract_path.read_text(encoding="utf-8"))

    variants = load_candidates(args.candidates, part=args.part)
    if not variants:
        print(f"no candidates for part {args.part!r} in {args.candidates}")
        return 1

    out_dir: Path = args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    turn_id = f"turn-{args.round_id}"
    stores = build_stores(out_dir)
    sanitizer = default_sanitizer(contract, tokens_css=contract.to_css_custom_properties())
    judges = dry_panel() if args.dry_judges else cli_judges.default_panel()
    part_spec = args.part_spec.read_text(encoding="utf-8") if args.part_spec else ""
    anti_corpus = load_anti_corpus(args.anti_corpus, contract, on_error=print)

    with previews(args.candidates, stores.evidence, contract, turn_id) as base_url:
        with playwright_sensor.PlaywrightSensor(artifacts_dir=out_dir / "artifacts") as sensor:
            result = run_round(
                variants,
                contract=contract,
                stores=stores,
                sanitizer=sanitizer,
                sensor=sensor,
                judges=judges,
                round_id=str(args.round_id),
                out_dir=out_dir,
                url_for=lambda variant: f"{base_url}/candidate/{variant.variant_id}",
                part=args.part,
                part_spec=part_spec,
                turn_id=turn_id,
                anti_corpus=anti_corpus,
                max_workers=max(1, args.max_workers),
            )
    print(result.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
