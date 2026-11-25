import argparse
import os
import sys
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

from qc_platform.config import DATABASE_URL
from qc_platform.config import API_KEY_TTL_DAYS
from qc_platform.database import make_engine, make_session_factory
from qc_platform.models import APIKey, Tenant, utc_now
from qc_platform.schemas import TenantCreate
from qc_platform.security import generate_api_key, hash_api_key


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a tenant and its initial admin API key.")
    parser.add_argument("--name", required=True, help="Tenant display name")
    parser.add_argument("--slug", required=True, help="Unique lowercase tenant slug")
    parser.add_argument(
        "--key-file",
        type=Path,
        help="Create the administrator key with mode 0600 instead of printing its value",
    )
    args = parser.parse_args()
    tenant_details = TenantCreate(name=args.name, slug=args.slug)

    engine = make_engine(DATABASE_URL)
    factory = make_session_factory(engine)
    with factory() as session:
        existing = session.scalar(select(Tenant.id).where(Tenant.slug == tenant_details.slug))
        if existing:
            print(f"Tenant slug already exists: {tenant_details.slug}", file=sys.stderr)
            return 1
        tenant = Tenant(name=tenant_details.name, slug=tenant_details.slug)
        token = generate_api_key()
        session.add(tenant)
        session.flush()
        session.add(
            APIKey(
                tenant_id=tenant.id,
                name="Initial tenant administrator",
                role="tenant_admin",
                key_hash=hash_api_key(token),
                expires_at=utc_now() + timedelta(days=API_KEY_TTL_DAYS),
            )
        )
        session.commit()
    if args.key_file:
        descriptor = os.open(args.key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as key_file:
            key_file.write(token + "\n")
    print(f"Tenant created: {tenant_details.slug}")
    if args.key_file:
        print(f"Administrator API key stored at {args.key_file} with owner-only permissions.")
    else:
        print("Administrator API key (shown once; store it securely):")
        print(token)
    engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
