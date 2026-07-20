from source_status import business_day_age, make_source_status, summarize_alignment


def test_business_day_age_excludes_weekend():
    assert business_day_age('2026-07-10', as_of='2026-07-13') == 1


def test_alignment_reports_stale_and_mismatched_sources():
    statuses = [
        make_source_status('portfolio', '2026-07-08', as_of='2026-07-13').to_dict() | {'label': 'Portfolio'},
        make_source_status('curves', '2026-06-19', as_of='2026-07-13').to_dict() | {'label': 'Curves'},
    ]
    summary = summarize_alignment(statuses)
    assert summary['tone'] == 'warning'
    assert summary['misaligned'] is True
    assert set(summary['stale_labels']) == {'Portfolio', 'Curves'}
