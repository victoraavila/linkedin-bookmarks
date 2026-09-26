"""Synthetic Voyager responses shaped like LinkedIn's normalized+json format.

These are not captured from LinkedIn; they encode the entity types the parser
looks for (``UpdateV2``, ``SocialActivityCounts``) so the parsing contract is
locked down. Real captures can be dropped in via ``lbm`` debug tooling later.
"""

POST_ID = "7123456789012345678"
OTHER_POST_ID = "7123456789012345679"

FEED_UPDATE = {
    "data": {"*elements": [f"urn:li:activity:{POST_ID}"]},
    "included": [
        {
            "$type": "com.linkedin.voyager.feed.render.UpdateV2",
            "actor": {
                "name": {"text": "Jane Doe"},
                "description": {"text": "Head of Growth at Acme"},
                "urn": "urn:li:member:1001",
                "image": {
                    "vectorImage": {
                        "rootUrl": "https://media.licdn.com/",
                        "artifacts": [
                            {"width": 100, "fileIdentifyingUrlPathSegment": "small.jpg"},
                            {"width": 400, "fileIdentifyingUrlPathSegment": "big.jpg"},
                        ],
                    }
                },
            },
            "commentary": {
                "text": {
                    "text": (
                        "Pricing teardown: value-based pricing beats seat-based "
                        "most of the time. https://example.com/pricing #pricing @sam"
                    )
                }
            },
            "content": {
                "articleComponent": {
                    "title": "The pricing playbook",
                    "subtitle": "A field guide",
                    "navigationUrl": "https://example.com/playbook",
                },
                "media": {
                    "type": "image",
                    "vectorImage": {
                        "rootUrl": "https://media.licdn.com/",
                        "artifacts": [
                            {"width": 800, "fileIdentifyingUrlPathSegment": "post.jpg"}
                        ],
                    },
                },
            },
            "socialDetail": {
                "totalSocialActivityCounts": {
                    "numLikes": 42,
                    "numComments": 7,
                    "numShares": 3,
                }
            },
            "metadata": {"backendUrn": f"urn:li:activity:{POST_ID}"},
        },
        {
            "$type": "com.linkedin.voyager.feed.Comment",
            "commenterForDashConversion": {"title": {"text": "Sam"}},
            "comment": {"values": [{"value": "Great post"}]},
            "permalink": "https://www.linkedin.com/feed/update/",
            "createdTime": 1700000000000,
        },
    ],
}

# A post whose only handle signal is the URL slug, as in the saved-posts listing.
LISTING_UPDATE = {
    "$type": "com.linkedin.voyager.feed.render.UpdateV2",
    "actor": {"name": {"text": "Alex Kim"}, "urn": "urn:li:member:2002"},
    "commentary": {"text": {"text": "Reposting this great thread on evals."}},
    "content": {
        "navigationUrl": (
            "https://www.linkedin.com/posts/alex-kim_evals-activity-"
            f"{OTHER_POST_ID}"
        )
    },
    "metadata": {"backendUrn": f"urn:li:activity:{OTHER_POST_ID}"},
}

SAVED_PAGE_1 = {
    "data": {
        "searchDashClustersByAll": {
            "metadata": {"paginationToken": "TOKEN-A"},
            "elements": [
                {
                    "items": [
                        {
                            "item": {
                                "entityResult": {
                                    "entityUrn": f"urn:li:activity:{POST_ID}"
                                }
                            }
                        }
                    ]
                }
            ],
        }
    },
    "included": [FEED_UPDATE["included"][0]],
}

SAVED_PAGE_2 = {
    "data": {
        "searchDashClustersByAll": {
            "metadata": {"paginationToken": "TOKEN-B"},
            "elements": [
                {
                    "items": [
                        {
                            "item": {
                                "entityResult": {
                                    "entityUrn": f"urn:li:activity:{OTHER_POST_ID}"
                                }
                            }
                        }
                    ]
                }
            ],
        }
    },
    "included": [LISTING_UPDATE],
}

SAVED_PAGE_3_EMPTY = {
    "data": {
        "searchDashClustersByAll": {
            "metadata": {"paginationToken": "TOKEN-C"},
            "elements": [],
        }
    },
    "included": [],
}
