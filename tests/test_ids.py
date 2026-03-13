from imghost.ids import ALPHABET, generate_album_id, generate_media_id, is_valid_id


def test_generated_ids_match_design_lengths() -> None:
    album_id = generate_album_id()
    media_id = generate_media_id()

    assert len(album_id) == 9
    assert len(media_id) == 12
    assert all(char in ALPHABET for char in album_id)
    assert all(char in ALPHABET for char in media_id)
    assert is_valid_id(album_id, 9)
    assert is_valid_id(media_id, 12)

