import pytest
from models import User, ForumPost, ForumComment, ForumPostLike, ForumNotification
from auth import get_current_user, get_current_user_optional

@pytest.fixture(autouse=True)
def mock_auth(db):
    user = User(
        id="user-1",
        email="test_forum@example.com",
        username="ForumTester",
        picture="http://example.com/avatar.png",
        terms_agreed=True
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    from main import app
    def mock_get_current_user():
        return user
    def mock_get_current_user_optional():
        return user
        
    app.dependency_overrides[get_current_user] = mock_get_current_user
    app.dependency_overrides[get_current_user_optional] = mock_get_current_user_optional
    yield
    app.dependency_overrides.clear()

def test_create_post(client, db):
    payload = {
        "title": "Test Slicing Profile",
        "content": "This is a test description of the slicer parameters.",
        "category": "discussions",
        "body_type": "FDM",
        "multi_color_type": "Stickers",
        "image": "http://example.com/image.png"
    }
    response = client.post("/skin/api/forum/posts", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["title"] == "Test Slicing Profile"
    assert data["category"] == "discussions"
    assert "FDM" in data["tags"]
    assert data["author"] == "ForumTester"
    assert data["image"] == "http://example.com/image.png"

    # Verify in DB
    post = db.query(ForumPost).filter(ForumPost.id == data["id"]).first()
    assert post is not None
    assert post.title == "Test Slicing Profile"


def test_create_post_rejects_raw_html(client):
    payload = {
        "title": "Unsafe HTML",
        "content": '<img src="x" onerror="alert(1)">',
        "category": "discussions",
    }

    response = client.post("/skin/api/forum/posts", json=payload)
    assert response.status_code == 400
    assert response.json()["detail"] == "Raw HTML is not allowed. / 帖子中不允许包含 HTML。"


def test_presigned_upload_rejects_svg(client):
    response = client.post(
        "/skin/api/upload/presigned-url",
        json={"filename": "payload.svg", "content_type": "image/svg+xml"}
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Only PNG, JPEG, WebP, and GIF uploads are allowed"

def test_list_posts(client, db):
    # Seed posts
    post1 = ForumPost(
        title="Post 1",
        content="Description 1",
        category="discussions",
        user_id="user-1",
        tags=["FDM"]
    )
    post2 = ForumPost(
        title="Post 2",
        content="Description 2",
        category="showcase",
        user_id="user-1",
        tags=["SLA"],
        image="http://example.com/p2.png"
    )
    db.add_all([post1, post2])
    db.commit()

    response = client.get("/skin/api/forum/posts")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 2
    assert len(data["posts"]) == 2

    # Filter by category
    response = client.get("/skin/api/forum/posts?category=showcase")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert len(data["posts"]) == 1
    assert data["posts"][0]["title"] == "Post 2"

def test_get_post(client, db):
    post = ForumPost(
        title="View Count Post",
        content="Content info",
        category="discussions",
        user_id="user-1"
    )
    db.add(post)
    db.commit()
    db.refresh(post)

    response = client.get(f"/skin/api/forum/posts/{post.id}")
    assert response.status_code == 200
    data = response.json()
    assert data["title"] == "View Count Post"
    assert data["views"] == 1

    # Verify view count incremented in DB
    db.refresh(post)
    assert post.views_count == 1

def test_like_post(client, db):
    # Post authored by someone else to verify notification creation
    other_user = User(
        id="user-2",
        email="other@example.com",
        username="OtherUser"
    )
    db.add(other_user)
    db.commit()

    post = ForumPost(
        title="Likeable Post",
        content="Content info",
        category="discussions",
        user_id="user-2"
    )
    db.add(post)
    db.commit()
    db.refresh(post)

    # Like
    response = client.post(f"/skin/api/forum/posts/{post.id}/like")
    assert response.status_code == 200
    assert response.json()["isLiked"] is True
    assert response.json()["likes"] == 1

    db.refresh(post)
    assert post.likes_count == 1

    # Check notification created for user-2
    notif = db.query(ForumNotification).filter(
        ForumNotification.user_id == "user-2",
        ForumNotification.sender_id == "user-1",
        ForumNotification.type == "like"
    ).first()
    assert notif is not None

    # Unlike
    response = client.post(f"/skin/api/forum/posts/{post.id}/like")
    assert response.status_code == 200
    assert response.json()["isLiked"] is False
    assert response.json()["likes"] == 0

    db.refresh(post)
    assert post.likes_count == 0

    # Check notification is cleaned up
    notif = db.query(ForumNotification).filter(
        ForumNotification.user_id == "user-2",
        ForumNotification.sender_id == "user-1",
        ForumNotification.type == "like"
    ).first()
    assert notif is None

def test_create_comment_and_reply(client, db):
    other_user = User(
        id="user-2",
        email="other@example.com",
        username="OtherUser"
    )
    db.add(other_user)
    db.commit()

    post = ForumPost(
        title="Commentable Post",
        content="Content info",
        category="discussions",
        user_id="user-2"
    )
    db.add(post)
    db.commit()
    db.refresh(post)

    # 1. Top level comment
    response = client.post(
        f"/skin/api/forum/posts/{post.id}/comments",
        json={"content": "Top-level comment content"}
    )
    assert response.status_code == 200
    comment_data = response.json()
    assert comment_data["content"] == "Top-level comment content"
    assert comment_data["author"] == "ForumTester"

    # Verify notification created for post author (user-2)
    notif = db.query(ForumNotification).filter(
        ForumNotification.user_id == "user-2",
        ForumNotification.sender_id == "user-1",
        ForumNotification.type == "comment"
    ).first()
    assert notif is not None

    # 2. Nested reply to top level comment
    response = client.post(
        f"/skin/api/forum/posts/{post.id}/comments",
        json={
            "content": "Nested reply content",
            "parent_id": comment_data["id"]
        }
    )
    assert response.status_code == 200
    reply_data = response.json()
    assert reply_data["content"] == "Nested reply content"

    # Verify notification created for top level comment author (user-1 is both commenter and replier here,
    # but let's confirm the DB structure anyway. Since user-1 replied to themselves in this test,
    # it won't create a notification because parent_comment.user_id (user-1) == current_user.id (user-1).)
    comment_in_db = db.query(ForumComment).filter(ForumComment.id == reply_data["id"]).first()
    assert comment_in_db is not None
    assert comment_in_db.parent_id == comment_data["id"]

def test_notifications_lifecycle(client, db):
    import datetime
    # Seed a notification for user-1
    notif1 = ForumNotification(
        user_id="user-1",
        sender_id="user-2",
        type="like",
        is_read=False,
        created_at=datetime.datetime.now(datetime.timezone.utc)
    )
    notif2 = ForumNotification(
        user_id="user-1",
        sender_id="user-2",
        type="comment",
        is_read=True,
        created_at=datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=1)
    )
    db.add_all([notif1, notif2])
    db.commit()

    # List notifications
    response = client.get("/skin/api/forum/notifications")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 2
    assert data["unread_count"] == 1
    assert len(data["notifications"]) == 2
    assert data["notifications"][0]["isRead"] is False # latest first (we ordered by desc)

    # Read all
    response = client.post("/skin/api/forum/notifications/read-all")
    assert response.status_code == 200
    assert response.json()["status"] == "success"

    # Verify in DB
    db.refresh(notif1)
    assert notif1.is_read is True

    # Test single notification read endpoint
    notif1.is_read = False
    db.commit()
    
    response = client.post(f"/skin/api/forum/notifications/{notif1.id}/read")
    assert response.status_code == 200
    assert response.json()["status"] == "success"
    
    db.refresh(notif1)
    assert notif1.is_read is True


def test_list_comments_paginated(client, db):
    post = ForumPost(
        title="Comments Test Post",
        content="Content info",
        category="discussions",
        user_id="user-1"
    )
    db.add(post)
    db.commit()
    db.refresh(post)

    # Add 12 top-level comments and some replies to see if pagination works
    comments = []
    for i in range(12):
        c = ForumComment(
            post_id=post.id,
            user_id="user-1",
            content=f"Root comment {i}"
        )
        comments.append(c)
    db.add_all(comments)
    db.commit()
    
    # Refresh to get IDs
    for c in comments:
        db.refresh(c)
        
    # Add a reply to the first root comment
    reply = ForumComment(
        post_id=post.id,
        parent_id=comments[0].id,
        user_id="user-1",
        content="Reply to first comment"
    )
    db.add(reply)
    db.commit()

    # Query page 1 (default page_size = 10)
    response = client.get(f"/skin/api/forum/posts/{post.id}/comments?page=1&page_size=10")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 12
    assert data["page"] == 1
    assert data["page_size"] == 10
    assert len(data["comments"]) == 10
    # First comment should have the reply nested
    assert data["comments"][0]["content"] == "Root comment 0"
    assert len(data["comments"][0]["replies"]) == 1
    assert data["comments"][0]["replies"][0]["content"] == "Reply to first comment"

    # Query page 2
    response = client.get(f"/skin/api/forum/posts/{post.id}/comments?page=2&page_size=10")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 12
    assert data["page"] == 2
    assert len(data["comments"]) == 2
    assert data["comments"][0]["content"] == "Root comment 10"


def test_create_post_confirm_temp_images(client, db):
    from unittest.mock import patch
    with patch("routers.forum.s3_client") as mock_s3:
        payload = {
            "title": "Temp Image Post",
            "content": "Check out this image: ![figure](http://cdn.example.com/forum/temp/abc123xyz.png)!",
            "category": "discussions",
            "body_type": "FDM",
            "multi_color_type": "Stickers",
            "image": "http://cdn.example.com/forum/temp/thumb456.jpg"
        }
        response = client.post("/skin/api/forum/posts", json=payload)
        assert response.status_code == 200
        data = response.json()
        
        # Verify URLs were rewritten
        assert "forum/active/abc123xyz.png" in data["content"]
        assert "forum/temp/" not in data["content"]
        assert data["image"] == "http://cdn.example.com/forum/active/thumb456.jpg"
        
        # Verify S3 client was called correctly for copying and deleting
        # There should be two files to copy and delete: abc123xyz.png and thumb456.jpg
        assert mock_s3.copy_object.call_count == 2
        assert mock_s3.delete_object.call_count == 2
        
        # Check copy_object call arguments
        calls_copy = [c[1] for c in mock_s3.copy_object.call_args_list]
        keys_copied = {c["Key"] for c in calls_copy}
        assert keys_copied == {"forum/active/abc123xyz.png", "forum/active/thumb456.jpg"}
        
        # Check delete_object call arguments
        calls_delete = [c[1] for c in mock_s3.delete_object.call_args_list]
        keys_deleted = {c["Key"] for c in calls_delete}
        assert keys_deleted == {"forum/temp/abc123xyz.png", "forum/temp/thumb456.jpg"}


def test_create_showcase_post_without_image_fails(client, db):
    payload = {
        "title": "Showcase Without Image",
        "content": "This is a post without any images in showcase.",
        "category": "showcase",
        "body_type": "FDM",
        "multi_color_type": "Stickers"
    }
    response = client.post("/skin/api/forum/posts", json=payload)
    assert response.status_code == 400
    assert "Showcase posts must contain at least one image" in response.json()["detail"]


def test_create_showcase_post_with_image_succeeds(client, db):
    from unittest.mock import patch
    with patch("routers.forum.s3_client") as mock_s3:
        payload = {
            "title": "Showcase With Image",
            "content": "This is a post with an image: ![printed figure](http://example.com/forum/temp/img123.jpg)",
            "category": "showcase",
            "body_type": "FDM",
            "multi_color_type": "Stickers",
            "image": "http://example.com/forum/temp/img123.jpg"
        }
        response = client.post("/skin/api/forum/posts", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "Showcase With Image"
        assert "forum/active/img123.jpg" in data["content"]


def test_create_post_rate_limit(client, db):
    from routers.forum import redis_client
    redis_client._store.clear()
    
    # 5 posts should succeed
    for i in range(5):
        payload = {
            "title": f"Post {i}",
            "content": f"Content {i}",
            "category": "discussions"
        }
        response = client.post("/skin/api/forum/posts", json=payload)
        assert response.status_code == 200
        
    # The 6th post should fail with 429
    payload = {
        "title": "Post 6",
        "content": "Content 6",
        "category": "discussions"
    }
    response = client.post("/skin/api/forum/posts", json=payload)
    assert response.status_code == 429
    assert "Daily limit of 5 posts exceeded" in response.json()["detail"]


def test_create_comment_rate_limit(client, db):
    from routers.forum import redis_client
    redis_client._store.clear()
    
    post = ForumPost(
        title="Post for Comment Rate Limit",
        content="Content info",
        category="discussions",
        user_id="user-1"
    )
    db.add(post)
    db.commit()
    db.refresh(post)
    
    # 50 comments should succeed
    for i in range(50):
        response = client.post(
            f"/skin/api/forum/posts/{post.id}/comments",
            json={"content": f"Comment {i}"}
        )
        assert response.status_code == 200
        
    # The 51st comment should fail with 429
    response = client.post(
        f"/skin/api/forum/posts/{post.id}/comments",
        json={"content": "Comment 51"}
    )
    assert response.status_code == 429
    assert "Daily limit of 50 comments/replies exceeded" in response.json()["detail"]


def test_upload_presigned_url_rate_limit(client, db):
    from routers.forum import redis_client
    redis_client._store.clear()
    
    from unittest.mock import patch
    with patch("routers.forum.s3_client") as mock_s3:
        mock_s3.generate_presigned_post.return_value = {"url": "http://mock-s3-upload", "fields": {}}
        
        # 50 uploads should succeed
        for i in range(50):
            response = client.post(
                "/skin/api/upload/presigned-url",
                json={"filename": f"test{i}.png", "content_type": "image/png"}
            )
            assert response.status_code == 200
            
        # The 51st upload should fail with 429
        response = client.post(
            "/skin/api/upload/presigned-url",
            json={"filename": "test51.png", "content_type": "image/png"}
        )
        assert response.status_code == 429
        assert "Daily limit of 50 uploads exceeded" in response.json()["detail"]


def test_list_videos_empty(client, db):
    from models import ForumVideo
    
    # 1. Fetching when empty should return empty list and not trigger seeding
    response = client.get("/skin/api/forum/videos")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 0
    
    # Check that nothing exists in DB
    count = db.query(ForumVideo).count()
    assert count == 0


def test_list_videos_returns_existing(client, db):
    from models import ForumVideo
    # Seed a custom video directly in the DB
    video = ForumVideo(
        youtube_id="abcdefghijk"
    )
    db.add(video)
    db.commit()
    
    # Fetching should return the custom video and not seed default ones
    response = client.get("/skin/api/forum/videos")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["youtubeId"] == "abcdefghijk"


def test_create_video_unauthorized(client):
    payload = {
        "youtube_url": "https://www.youtube.com/watch?v=12345678901"
    }
    # Standard user has no admin overrides, so it should fail
    response = client.post("/skin/api/forum/videos", json=payload)
    assert response.status_code == 403


def test_create_video_admin_success(client, db):
    from models import ForumVideo, User
    from auth import get_current_admin
    from main import app
    
    # 1. Create a mock admin user
    admin_user = User(
        id="admin-user",
        email="admin@example.com",
        username="AdminUser",
        terms_agreed=True
    )
    db.add(admin_user)
    db.commit()
    
    # 2. Mock admin authorization
    def mock_get_current_admin():
        return admin_user
    app.dependency_overrides[get_current_admin] = mock_get_current_admin
    
    try:
        payload = {
            "youtube_url": "https://www.youtube.com/watch?v=12345678901"
        }
        response = client.post("/skin/api/forum/videos", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["youtubeId"] == "12345678901"
        
        # Verify in DB
        db_video = db.query(ForumVideo).filter(ForumVideo.id == data["id"]).first()
        assert db_video is not None
        assert db_video.youtube_id == "12345678901"
    finally:
        if get_current_admin in app.dependency_overrides:
            del app.dependency_overrides[get_current_admin]


def test_create_video_invalid_youtube(client, db):
    from models import User
    from auth import get_current_admin
    from main import app
    
    # 1. Create a mock admin user
    admin_user = User(
        id="admin-user",
        email="admin@example.com",
        username="AdminUser",
        terms_agreed=True
    )
    db.add(admin_user)
    db.commit()
    
    # 2. Mock admin authorization
    def mock_get_current_admin():
        return admin_user
    app.dependency_overrides[get_current_admin] = mock_get_current_admin
    
    try:
        payload = {
            "youtube_url": "https://www.google.com"
        }
        response = client.post("/skin/api/forum/videos", json=payload)
        assert response.status_code == 400
        assert "Invalid YouTube URL or ID." in response.json()["detail"]
    finally:
        if get_current_admin in app.dependency_overrides:
            del app.dependency_overrides[get_current_admin]


def test_delete_video_unauthorized(client, db):
    from models import ForumVideo
    
    # Seed a video
    video = ForumVideo(
        youtube_id="12345678901"
    )
    db.add(video)
    db.commit()
    
    response = client.delete(f"/skin/api/forum/videos/{video.id}")
    assert response.status_code == 403


def test_delete_video_admin_success(client, db):
    from models import ForumVideo, User
    from auth import get_current_admin
    from main import app
    
    # Seed a video
    video = ForumVideo(
        youtube_id="12345678901"
    )
    db.add(video)
    
    admin_user = User(
        id="admin-user",
        email="admin@example.com",
        username="AdminUser",
        terms_agreed=True
    )
    db.add(admin_user)
    db.commit()
    
    def mock_get_current_admin():
        return admin_user
    app.dependency_overrides[get_current_admin] = mock_get_current_admin
    
    try:
        response = client.delete(f"/skin/api/forum/videos/{video.id}")
        assert response.status_code == 200
        assert response.json() == {"status": "success"}
        
        # Verify deleted in DB
        db_video = db.query(ForumVideo).filter(ForumVideo.id == video.id).first()
        assert db_video is None
    finally:
        if get_current_admin in app.dependency_overrides:
            del app.dependency_overrides[get_current_admin]


def test_delete_video_not_found(client, db):
    from models import User
    from auth import get_current_admin
    from main import app
    
    admin_user = User(
        id="admin-user",
        email="admin@example.com",
        username="AdminUser",
        terms_agreed=True
    )
    db.add(admin_user)
    db.commit()
    
    def mock_get_current_admin():
        return admin_user
    app.dependency_overrides[get_current_admin] = mock_get_current_admin
    
    try:
        response = client.delete("/skin/api/forum/videos/non_existent_id")
        assert response.status_code == 404
        assert "Video not found" in response.json()["detail"]
    finally:
        if get_current_admin in app.dependency_overrides:
            del app.dependency_overrides[get_current_admin]


def test_create_post_markdown_link_rejected(client):
    payload = {
        "title": "Link Post Rejected",
        "content": "Check out [this link](https://malicious.com) please!",
        "category": "discussions"
    }
    response = client.post("/skin/api/forum/posts", json=payload)
    assert response.status_code == 400
    assert "Clickable links are not allowed" in response.json()["detail"]


def test_create_post_html_link_rejected(client):
    payload = {
        "title": "HTML Link Rejected",
        "content": "Visit <a href='https://malicious.com'>our shop</a>",
        "category": "discussions"
    }
    response = client.post("/skin/api/forum/posts", json=payload)
    assert response.status_code == 400
    assert "Raw HTML is not allowed" in response.json()["detail"]


def test_create_post_raw_url_rejected(client):
    payload = {
        "title": "Raw URL Rejected",
        "content": "Go to https://malicious.com for details",
        "category": "discussions"
    }
    response = client.post("/skin/api/forum/posts", json=payload)
    assert response.status_code == 400
    assert "Raw URLs are not allowed" in response.json()["detail"]


def test_create_post_external_image_rejected(client):
    payload = {
        "title": "External Image Rejected",
        "content": "Look at this external image: ![alt](https://malicious-site.com/image.png)",
        "category": "discussions"
    }
    response = client.post("/skin/api/forum/posts", json=payload)
    assert response.status_code == 400
    assert "External images are not allowed" in response.json()["detail"]


def test_create_post_internal_image_allowed(client, db):
    payload = {
        "title": "Internal Image Allowed",
        "content": "Look at this internal image: ![alt](http://example.com/forum/temp/image.png)",
        "category": "discussions"
    }
    from unittest.mock import patch
    with patch("routers.forum.s3_client") as mock_s3:
        response = client.post("/skin/api/forum/posts", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert "forum/active/image.png" in data["content"]


def test_list_notifications_rate_limit(client, db):
    from routers.forum import redis_client
    redis_client._store.clear()
    
    # Simulate user-1 already having checked notifications 60 times in this minute
    redis_client.set("rl:notifications:60:user-1", 60)
    
    # The 61st request should fail with 429
    response = client.get("/skin/api/forum/notifications")
    assert response.status_code == 429
    assert "Limit of 60 notification checks per minute exceeded" in response.json()["detail"]


def test_delete_post_own_success(client, db):
    post = ForumPost(
        title="My Own Post",
        content="Delete this content",
        category="discussions",
        user_id="user-1"
    )
    db.add(post)
    db.commit()
    db.refresh(post)
    
    response = client.delete(f"/skin/api/forum/posts/{post.id}")
    assert response.status_code == 200
    assert response.json()["status"] == "success"
    assert db.query(ForumPost).filter(ForumPost.id == post.id).first() is None


def test_delete_post_admin_success(client, db):
    post = ForumPost(
        title="Other User Post",
        content="Delete this content",
        category="discussions",
        user_id="user-2"
    )
    db.add(post)
    db.commit()
    db.refresh(post)
    
    user = db.query(User).filter(User.id == "user-1").first()
    from config import settings
    original_admin_emails = settings.ADMIN_EMAILS
    settings.ADMIN_EMAILS = user.email
    
    try:
        response = client.delete(f"/skin/api/forum/posts/{post.id}")
        assert response.status_code == 200
        assert response.json()["status"] == "success"
        assert db.query(ForumPost).filter(ForumPost.id == post.id).first() is None
    finally:
        settings.ADMIN_EMAILS = original_admin_emails


def test_delete_post_forbidden(client, db):
    post = ForumPost(
        title="Other User Post",
        content="Delete this content",
        category="discussions",
        user_id="user-2"
    )
    db.add(post)
    db.commit()
    db.refresh(post)
    
    response = client.delete(f"/skin/api/forum/posts/{post.id}")
    assert response.status_code == 403
    assert "You do not have permission to delete this post" in response.json()["detail"]
    assert db.query(ForumPost).filter(ForumPost.id == post.id).first() is not None


def test_update_post_category_own_success(client, db):
    post = ForumPost(
        title="My Post",
        content="This is some content with an image: ![alt](http://example.com/forum/temp/image.png)",
        category="discussions",
        user_id="user-1"
    )
    db.add(post)
    db.commit()
    db.refresh(post)
    
    from unittest.mock import patch
    with patch("routers.forum.s3_client") as mock_s3:
        response = client.patch(f"/skin/api/forum/posts/{post.id}", json={"category": "showcase"})
        assert response.status_code == 200
        assert response.json()["category"] == "showcase"


def test_update_post_category_showcase_fails_without_image(client, db):
    post = ForumPost(
        title="My Post No Image",
        content="This content does not have any image.",
        category="discussions",
        user_id="user-1"
    )
    db.add(post)
    db.commit()
    db.refresh(post)
    
    response = client.patch(f"/skin/api/forum/posts/{post.id}", json={"category": "showcase"})
    assert response.status_code == 400
    assert "Showcase posts must contain at least one image" in response.json()["detail"]


def test_update_post_category_forbidden(client, db):
    post = ForumPost(
        title="Other User Post",
        content="Content info",
        category="discussions",
        user_id="user-2"
    )
    db.add(post)
    db.commit()
    db.refresh(post)
    
    response = client.patch(f"/skin/api/forum/posts/{post.id}", json={"category": "showcase"})
    assert response.status_code == 403
    assert "You do not have permission to update this post" in response.json()["detail"]





