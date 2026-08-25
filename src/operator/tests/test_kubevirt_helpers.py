from helpers.kubevirt import golden_import_matches, s3_import_url


def test_s3_import_url_aws_style():
    url = s3_import_url(
        "library/rhel-9.6.qcow2",
        {"bucket": "troshka-images", "region": "us-east-1"},
    )
    assert url == "https://s3.us-east-1.amazonaws.com/troshka-images/library/rhel-9.6.qcow2"


def test_s3_import_url_custom_endpoint():
    url = s3_import_url(
        "library/rhel-9.6.qcow2",
        {
            "bucket": "troshka-gold-images",
            "endpoint": "https://s4.example.com",
        },
    )
    assert url == "https://s4.example.com/troshka-gold-images/library/rhel-9.6.qcow2"


def test_golden_import_matches_true():
    cfg = {"bucket": "troshka-gold-images", "endpoint": "https://s4.example.com"}
    path = "library/rhel-9.6.qcow2"
    dv = {
        "spec": {
            "source": {
                "s3": {
                    "url": s3_import_url(path, cfg),
                    "secretRef": "s3-central-credentials",  # pragma: allowlist secret
                }
            }
        }
    }
    assert golden_import_matches(dv, path, cfg, "s3-central-credentials")


def test_golden_import_matches_false_on_bucket():
    cfg = {"bucket": "troshka-gold-images", "endpoint": "https://s4.example.com"}
    path = "library/rhel-9.6.qcow2"
    dv = {
        "spec": {
            "source": {
                "s3": {
                    "url": "https://s3.us-east-1.amazonaws.com/troshka-images/library/rhel-9.6.qcow2",
                    "secretRef": "s3-central-credentials",  # pragma: allowlist secret
                }
            }
        }
    }
    assert not golden_import_matches(dv, path, cfg, "s3-central-credentials")
