from types import SimpleNamespace

from .datasets import dataset_revision


def test_immutable_preparation_revision_takes_precedence_over_old_collection():
    data = SimpleNamespace(metadata={'stock_universe_preparation': {'prepared_at': 'new-preparation'},
                                     'collected_at': 'old-collection'})
    assert dataset_revision(data) == 'new-preparation'


def test_collection_revision_is_fallback_not_market_reference_month():
    assert dataset_revision(SimpleNamespace(metadata={'collected_at': 'new-collection',
        'reference_date': '2026-09-30'})) == 'new-collection'
    assert dataset_revision(SimpleNamespace(metadata={'reference_date': '2026-09-30'})) is None


def test_absent_or_invalid_metadata_is_not_an_invented_revision():
    for data in (SimpleNamespace(), SimpleNamespace(metadata=None), SimpleNamespace(metadata={'collected_at': 123})):
        assert dataset_revision(data) is None
