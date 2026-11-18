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


def test_nvidia_configuration_uses_nemotron_defaults(monkeypatch):
    monkeypatch.delenv("BAIDU_QIANFAN_API_KEY", raising=False)
    monkeypatch.setenv("NVIDIA_API_KEY", "test-key")
    monkeypatch.setenv("BIOLAB_EMBEDDING_PROVIDER", "nvidia")
    monkeypatch.delenv("BIOLAB_FREE_EMBEDDINGS", raising=False)
    monkeypatch.delenv("BIOLAB_LOCAL_EMBEDDINGS", raising=False)

    assert embedding_client.nvidia_enabled()
    assert embedding_client.model_name() == "nvidia/nemotron-3.5-embed-1b"
    assert embedding_client.dimension() == 2048
    assert embedding_client.configured()


def test_explicit_provider_wins_over_fallback_flags(monkeypatch):
    monkeypatch.setenv("BIOLAB_EMBEDDING_PROVIDER", "nvidia")
    monkeypatch.setenv("BIOLAB_FREE_EMBEDDINGS", "1")
    monkeypatch.setenv("NVIDIA_API_KEY", "test-key")

    assert embedding_client.provider() == "nvidia"
    assert embedding_client.model_name() == "nvidia/nemotron-3.5-embed-1b"
    assert embedding_client.dimension() == 2048


def test_empty_embedding_batch_is_a_noop(monkeypatch):
    monkeypatch.setenv("BIOLAB_EMBEDDING_PROVIDER", "free")

    assert embedding_client.embed([]) == []
