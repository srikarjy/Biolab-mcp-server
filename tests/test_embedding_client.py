from biolab import embedding_client


def test_baidu_configuration_uses_qianfan_defaults(monkeypatch):
    monkeypatch.setenv("BAIDU_QIANFAN_API_KEY", "test-key")
    monkeypatch.delenv("BIOLAB_FREE_EMBEDDINGS", raising=False)
    monkeypatch.delenv("BIOLAB_LOCAL_EMBEDDINGS", raising=False)

    assert embedding_client.baidu_enabled()
    assert embedding_client.model_name() == "embedding-v1"
    assert embedding_client.dimension() == 384
    assert embedding_client.configured()


def test_free_embeddings_stay_available_without_provider_key(monkeypatch):
    monkeypatch.delenv("BAIDU_QIANFAN_API_KEY", raising=False)
    monkeypatch.setenv("BIOLAB_FREE_EMBEDDINGS", "1")
    vectors = embedding_client.embed(["KRAS study", "KRAS study"])

    assert len(vectors) == 2
    assert len(vectors[0]) == 384
    assert vectors[0] == vectors[1]
