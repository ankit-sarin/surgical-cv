import argparse
import sys

from pipeline.commands import concat, deid, verify, status, metadata

STAGES = ["intake", "concatenated", "deidentified", "verified", "failed"]


def build_parser():
    parser = argparse.ArgumentParser(
        prog="pipeline",
        description="Surgical OR video processing pipeline CLI.",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    p_concat = subparsers.add_parser("concat", help="Concatenate BDV segments into a PHI master.")
    p_concat.add_argument("--surgeon", required=True, help="Surgeon folder name (e.g. sarin, miller).")
    p_concat.set_defaults(handler=concat.handle)

    p_deid = subparsers.add_parser("deid", help="De-identify concatenated cases for a surgeon.")
    p_deid.add_argument("--surgeon", required=True, help="Surgeon folder name (e.g. sarin, miller).")
    p_deid.add_argument("--case", default=None, help="Process only this case (UCD-FIL-###). Optional; defaults to batch mode.")
    p_deid.set_defaults(handler=deid.handle)

    p_verify = subparsers.add_parser("verify", help="Verify a de-identified video.")
    p_verify.add_argument("deid_file", help="Path to the de-identified video.")
    p_verify.set_defaults(handler=verify.handle)

    p_status = subparsers.add_parser("status", help="Show pipeline state.")
    p_status.add_argument("--case", default=None, help="Filter by UCD-FIL-### case id.")
    p_status.add_argument("--stage", choices=STAGES, default=None, help="Filter by pipeline stage.")
    p_status.add_argument("--json", action="store_true", help="Emit JSON instead of human-readable output.")
    p_status.set_defaults(handler=status.handle)

    p_metadata = subparsers.add_parser("metadata", help="Show or edit case metadata.")
    p_metadata.add_argument("ucd_fil_id", help="Case id in UCD-FIL-### form.")
    group = p_metadata.add_mutually_exclusive_group()
    group.add_argument("--show", action="store_true", help="Show metadata (default).")
    group.add_argument("--edit", nargs=2, metavar=("FIELD", "VALUE"), default=None, help="Edit a metadata field.")
    p_metadata.add_argument("--confirm", action="store_true", help="Confirm edit (required with --edit).")
    p_metadata.set_defaults(handler=metadata.handle, _parser=p_metadata)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.subcommand == "metadata" and args.confirm and args.edit is None:
        args._parser.error("--confirm is only meaningful with --edit")

    rc = args.handler(args)
    sys.exit(rc if rc is not None else 0)
