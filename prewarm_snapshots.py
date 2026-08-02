"""Populate shared dashboard snapshots before Gunicorn accepts traffic."""

from __future__ import annotations

import json

from pages.vol_surface import prepare_vol_surface_snapshot
from source_status import load_dashboard_source_statuses


def main():
    surface_reference = prepare_vol_surface_snapshot(force=False)
    statuses = load_dashboard_source_statuses(force=False)
    print(
        json.dumps(
            {
                'event': 'dashboard_snapshots_prewarmed',
                'vol_surface': {
                    'schema_version': surface_reference.get('schema_version'),
                    'source_revision': surface_reference.get('source_revision'),
                    'snapshot_id': surface_reference.get('snapshot_id'),
                    'meta': surface_reference.get('meta'),
                },
                'source_status_count': len(statuses),
            },
            default=str,
            sort_keys=True,
        )
    )


if __name__ == '__main__':
    main()
