def handle(args):
    parts = [f"ucd_fil_id={args.ucd_fil_id}"]
    if args.edit is not None:
        field, value = args.edit
        parts.append(f"edit_field={field}")
        parts.append(f"edit_value={value}")
        parts.append(f"confirm={args.confirm}")
    else:
        parts.append(f"show={args.show or args.edit is None}")
    print(f"Not yet implemented: metadata ({', '.join(parts)})")
    return 0
