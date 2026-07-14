from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import desc, or_
from typing import List, Optional
from pydantic import BaseModel
import uuid
import os
import datetime

import models
import schemas
import auth
from database import get_db
from s3_utils import s3_client, get_cdn_url
from config import settings
from redis import Redis

redis_client = Redis.from_url(settings.REDIS_URL)
ALLOWED_FORUM_UPLOAD_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

def check_rate_limit(user_id: str, action: str, limit: int, err_msg: str, period: int = 86400):
    """
    Check rate limit for a user action and raise 429 if exceeded.
    """
    key = f"rl:{action}:{user_id}" if period == 86400 else f"rl:{action}:{period}:{user_id}"
    try:
        count = redis_client.incr(key)
        if count == 1:
            redis_client.expire(key, period)
        if count > limit:
            raise HTTPException(status_code=429, detail=err_msg)
    except HTTPException:
        raise
    except Exception as e:
        # Fail-open if Redis has issues (e.g. connection error)
        print(f"Redis rate limit check error for user {user_id}, action {action}: {e}")

router = APIRouter(prefix="/api", tags=["forum"])

class PresignedUrlRequest(BaseModel):
    filename: str
    content_type: str

@router.post("/upload/presigned-url")
async def get_presigned_url(
    req: PresignedUrlRequest,
    current_user: models.User = Depends(auth.get_current_user)
):
    """
    Generate a presigned S3 POST policy for client-side upload.
    Enforces a file size limit (max 512KB) directly in S3.
    """
    check_rate_limit(
        current_user.id,
        "upload",
        50,
        "Daily limit of 50 uploads exceeded. / 每日图片上传次数已达上限（50次）。"
    )
    content_type = req.content_type.split(";", 1)[0].strip().lower()
    if content_type not in ALLOWED_FORUM_UPLOAD_TYPES:
        raise HTTPException(status_code=400, detail="Only PNG, JPEG, WebP, and GIF uploads are allowed")

    # Generate a unique key/filename in the public S3 bucket
    file_ext = os.path.splitext(req.filename)[1].lower() if req.filename else ""
    if file_ext not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        file_ext = ALLOWED_FORUM_UPLOAD_TYPES[content_type]

    s3_id = uuid.uuid4().hex
    filename = f"forum/temp/{s3_id}{file_ext}"
    bucket = settings.AWS_BUCKET_NAME

    try:
        # Generate presigned POST policy with 512KB size limit
        post_data = s3_client.generate_presigned_post(
            Bucket=bucket,
            Key=filename,
            Fields={
                "acl": "public-read",
                "Content-Type": content_type
            },
            Conditions=[
                ["content-length-range", 0, 512 * 1024],  # Max 512KB limit
                {"acl": "public-read"},
                {"Content-Type": content_type}
            ],
            ExpiresIn=3600
        )
        
        file_url = get_cdn_url(filename, bucket=bucket)
        
        return {
            "uploadUrl": post_data["url"],
            "fields": post_data["fields"],
            "fileUrl": file_url
        }
    except Exception as e:
        print(f"Error generating presigned POST data: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to generate upload config: {str(e)}")


def get_relative_time(dt: datetime.datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    now = datetime.datetime.now(datetime.timezone.utc)
    diff = now - dt
    diff_sec = int(diff.total_seconds())
    if diff_sec < 60:
        return "Just now"
    diff_min = diff_sec // 60
    if diff_min < 60:
        return f"{diff_min}m ago"
    diff_hours = diff_min // 60
    if diff_hours < 24:
        return f"{diff_hours}h ago"
    diff_days = diff_hours // 24
    if diff_days < 30:
        return f"{diff_days}d ago"
    return dt.strftime("%Y-%m-%d")


def get_comment_tree(db: Session, post_id: str) -> List[schemas.ForumCommentResponse]:
    comments = db.query(models.ForumComment).filter(models.ForumComment.post_id == post_id).all()
    user_ids = {c.user_id for c in comments}
    users = db.query(models.User).filter(models.User.id.in_(user_ids)).all() if user_ids else []
    user_map = {u.id: u for u in users}

    def build_tree(parent_id: Optional[str] = None) -> List[schemas.ForumCommentResponse]:
        tree = []
        level_comments = [c for c in comments if c.parent_id == parent_id]
        level_comments.sort(key=lambda x: x.created_at)
        for c in level_comments:
            u = user_map.get(c.user_id)
            replies = build_tree(c.id)
            tree.append(schemas.ForumCommentResponse(
                id=c.id,
                author=u.username if u else "GuestMaker",
                avatarUrl=u.picture if u else None,
                minecraftSkinUrl=u.minecraft_skin_url if u else None,
                isPro=u.is_pro if u else False,
                content=c.content,
                createdAt=get_relative_time(c.created_at),
                replies=replies
            ))
        return tree

    return build_tree(None)


@router.get("/forum/posts", response_model=schemas.ForumPostsPaginatedResponse)
async def list_posts(
    category: Optional[str] = None,
    search: Optional[str] = None,
    sort: str = "latest",
    page: int = 1,
    page_size: int = 10,
    db: Session = Depends(get_db),
    current_user: Optional[models.User] = Depends(auth.get_current_user_optional)
):
    query = db.query(models.ForumPost)
    if category and category != "all" and category != "discussions":
        if category == "showcase":
            query = query.filter(models.ForumPost.category == "showcase")
    elif category == "discussions":
        query = query.filter(models.ForumPost.category == "discussions")

    if search:
        search_filter = or_(
            models.ForumPost.title.ilike(f"%{search}%"),
            models.ForumPost.content.ilike(f"%{search}%")
        )
        query = query.filter(search_filter)

    if sort == "popular":
        query = query.order_by(desc(models.ForumPost.likes_count), desc(models.ForumPost.created_at))
    else:
        query = query.order_by(desc(models.ForumPost.created_at))

    total = query.count()
    offset = (page - 1) * page_size
    posts = query.offset(offset).limit(page_size).all()

    user_ids = {p.user_id for p in posts}
    users = db.query(models.User).filter(models.User.id.in_(user_ids)).all() if user_ids else []
    user_map = {u.id: u for u in users}

    liked_post_ids = set()
    if current_user:
        likes = db.query(models.ForumPostLike).filter(
            models.ForumPostLike.user_id == current_user.id,
            models.ForumPostLike.post_id.in_([p.id for p in posts])
        ).all()
        liked_post_ids = {lk.post_id for lk in likes}

    post_ids = [p.id for p in posts]
    comment_counts = {}
    if post_ids:
        from sqlalchemy import func
        counts = db.query(
            models.ForumComment.post_id,
            func.count(models.ForumComment.id)
        ).filter(
            models.ForumComment.post_id.in_(post_ids)
        ).group_by(
            models.ForumComment.post_id
        ).all()
        comment_counts = {post_id: count for post_id, count in counts}

    res = []
    for p in posts:
        u = user_map.get(p.user_id)
        role = "Member"
        if u:
            if u.is_admin:
                role = "Mod"
            elif u.is_pro:
                role = "Pro Member"

        comments_tree = []

        res.append(schemas.ForumPostResponse(
            id=p.id,
            title=p.title,
            content=p.content,
            category=p.category,
            image=p.image,
            tags=p.tags or [],
            author=u.username if u else "GuestMaker",
            authorAvatar=u.picture if u else None,
            authorMinecraftSkinUrl=u.minecraft_skin_url if u else None,
            isPro=u.is_pro if u else False,
            role=role,
            likes=p.likes_count,
            views=p.views_count,
            isLiked=p.id in liked_post_ids,
            printSettings=schemas.PrintSettings(),
            comments=comments_tree,
            commentsCount=comment_counts.get(p.id, 0),
            createdAt=get_relative_time(p.created_at),
            bodyType=p.body_type,
            multiColorType=p.multi_color_type
        ))
    return schemas.ForumPostsPaginatedResponse(
        posts=res,
        total=total,
        page=page,
        page_size=page_size
    )


@router.get("/forum/posts/{post_id}", response_model=schemas.ForumPostResponse)
async def get_post(
    post_id: str,
    db: Session = Depends(get_db),
    current_user: Optional[models.User] = Depends(auth.get_current_user_optional)
):
    post = db.query(models.ForumPost).filter(models.ForumPost.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    post.views_count += 1
    db.commit()
    db.refresh(post)

    author = db.query(models.User).filter(models.User.id == post.user_id).first()
    role = "Member"
    if author:
        if author.is_admin:
            role = "Mod"
        elif author.is_pro:
            role = "Pro Member"

    is_liked = False
    if current_user:
        like = db.query(models.ForumPostLike).filter(
            models.ForumPostLike.user_id == current_user.id,
            models.ForumPostLike.post_id == post.id
        ).first()
        is_liked = like is not None

    comments_tree = []
    comments_count = db.query(models.ForumComment).filter(models.ForumComment.post_id == post.id).count()

    return schemas.ForumPostResponse(
        id=post.id,
        title=post.title,
        content=post.content,
        category=post.category,
        image=post.image,
        tags=post.tags or [],
        author=author.username if author else "GuestMaker",
        authorAvatar=author.picture if author else None,
        authorMinecraftSkinUrl=author.minecraft_skin_url if author else None,
        isPro=author.is_pro if author else False,
        role=role,
        likes=post.likes_count,
        views=post.views_count,
        isLiked=is_liked,
        printSettings=schemas.PrintSettings(),
        comments=comments_tree,
        commentsCount=comments_count,
        createdAt=get_relative_time(post.created_at),
        bodyType=post.body_type,
        multiColorType=post.multi_color_type
    )


@router.get("/forum/posts/{post_id}/comments", response_model=schemas.ForumCommentsPaginatedResponse)
async def list_comments(
    post_id: str,
    page: int = 1,
    page_size: int = 10,
    db: Session = Depends(get_db)
):
    # Get total count of first-level comments
    first_level_query = db.query(models.ForumComment).filter(
        models.ForumComment.post_id == post_id,
        models.ForumComment.parent_id == None
    )
    total = first_level_query.count()
    
    offset = (page - 1) * page_size
    first_level_comments = first_level_query.order_by(models.ForumComment.created_at.asc()).offset(offset).limit(page_size).all()
    
    # Get all replies (non-first-level comments) for this post
    replies = db.query(models.ForumComment).filter(
        models.ForumComment.post_id == post_id,
        models.ForumComment.parent_id != None
    ).all()
    
    # Combine first_level_comments and replies to build the tree
    all_comments = first_level_comments + replies
    
    user_ids = {c.user_id for c in all_comments}
    users = db.query(models.User).filter(models.User.id.in_(user_ids)).all() if user_ids else []
    user_map = {u.id: u for u in users}
    
    def build_tree(parent_id: Optional[str] = None, current_level_comments = None) -> List[schemas.ForumCommentResponse]:
        tree = []
        if current_level_comments is None:
            # Root level, use paginated first level comments
            level_comments = list(first_level_comments)
        else:
            level_comments = [c for c in replies if c.parent_id == parent_id]
            
        level_comments.sort(key=lambda x: x.created_at)
        for c in level_comments:
            u = user_map.get(c.user_id)
            child_replies = build_tree(c.id, current_level_comments=replies)
            tree.append(schemas.ForumCommentResponse(
                id=c.id,
                author=u.username if u else "GuestMaker",
                avatarUrl=u.picture if u else None,
                minecraftSkinUrl=u.minecraft_skin_url if u else None,
                isPro=u.is_pro if u else False,
                content=c.content,
                createdAt=get_relative_time(c.created_at),
                replies=child_replies
            ))
        return tree

    res = build_tree(None)
    return schemas.ForumCommentsPaginatedResponse(
        comments=res,
        total=total,
        page=page,
        page_size=page_size
    )


def confirm_temp_images(content: str, image_url: Optional[str] = None) -> tuple[str, Optional[str]]:
    """
    Finds any temporary S3 forum uploads in the content and image_url,
    copies them to the active/ permanent folder, deletes the temporary ones,
    and updates the URLs.
    """
    import re
    bucket = settings.AWS_BUCKET_NAME
    
    # Find all temp filenames in content
    content_filenames = re.findall(r'forum/temp/([a-zA-Z0-9]+\.[a-zA-Z0-9]+)', content)
    
    # Find if image_url is also temp
    image_filename = None
    if image_url:
        match = re.search(r'forum/temp/([a-zA-Z0-9]+\.[a-zA-Z0-9]+)', image_url)
        if match:
            image_filename = match.group(1)
            
    # Combine unique filenames to process
    all_filenames = list(set(content_filenames + ([image_filename] if image_filename else [])))
    
    # Move each file in S3
    for fname in all_filenames:
        src_key = f"forum/temp/{fname}"
        dest_key = f"forum/active/{fname}"
        try:
            s3_client.copy_object(
                Bucket=bucket,
                CopySource={'Bucket': bucket, 'Key': src_key},
                Key=dest_key,
                ACL='public-read'
            )
            s3_client.delete_object(Bucket=bucket, Key=src_key)
        except Exception as e:
            print(f"Failed to move S3 object {src_key} to {dest_key}: {e}")
            
    # Rewrite URLs in content and image_url
    new_content = content.replace("forum/temp/", "forum/active/")
    new_image_url = image_url.replace("forum/temp/", "forum/active/") if image_url else None
    
    return new_content, new_image_url


def validate_post_content(content: str, category: str):
    import re
    from urllib.parse import urlparse
    from config import settings
    
    # 1. Identify allowed hosts
    allowed_hosts = {"example.com", "cdn.example.com", "localhost"}
    if settings.AWS_CDN_DOMAIN:
        allowed_hosts.add(settings.AWS_CDN_DOMAIN.lower())
    if settings.AWS_BUCKET_NAME:
        bucket = settings.AWS_BUCKET_NAME.lower()
        allowed_hosts.add(f"{bucket}.s3.amazonaws.com")
        if settings.AWS_REGION:
            allowed_hosts.add(f"{bucket}.s3.{settings.AWS_REGION.lower()}.amazonaws.com")
            
    # 2. Raw HTML is not allowed in user-authored forum markdown.
    if re.search(r'<\s*/?\s*[a-zA-Z][^>]*>', content):
        raise HTTPException(
            status_code=400,
            detail="Raw HTML is not allowed. / 帖子中不允许包含 HTML。"
        )

    # 3. Extract markdown images: ![alt](url)
    md_images = re.findall(r'!\[.*?\]\((.*?)\)', content)

    all_images = md_images
    
    # 4. Showcase must have at least one image
    if category == "showcase" and not all_images:
        raise HTTPException(
            status_code=400,
            detail="Showcase posts must contain at least one image. / 玩家晒图必须包含至少一张图片。"
        )
    
    # 5. Check for external images
    for img_url in all_images:
        img_url = img_url.strip()
        parsed = urlparse(img_url)
        if parsed.netloc:
            host = parsed.netloc.lower()
            if ":" in host:
                host_name = host.split(":")[0]
            else:
                host_name = host
                
            if host_name not in allowed_hosts and not any(host_name.endswith(f".{h}") for h in allowed_hosts):
                raise HTTPException(
                    status_code=400,
                    detail="External images are not allowed. / 不允许使用外部图片。"
                )
                
    # 6. Remove all valid images from the content to inspect what's left
    temp_content = re.sub(r'!\[.*?\]\(.*?\)', '', content)
    
    # 7. Check for remaining markdown links: [text](url)
    if re.search(r'\[.*?\]\(.*?\)', temp_content):
        raise HTTPException(
            status_code=400,
            detail="Clickable links are not allowed. / 帖子中不允许包含可点击的链接。"
        )
        
    # 8. Check for HTML links: <a ...>
    if re.search(r'<a\s+.*?>', temp_content, re.IGNORECASE) or re.search(r'href\s*=', temp_content, re.IGNORECASE):
        raise HTTPException(
            status_code=400,
            detail="Clickable links are not allowed. / 帖子中不允许包含可点击的链接。"
        )
        
    # 9. Check for raw URLs: http:// or https:// or www.
    if re.search(r'https?://[^\s\)]+', temp_content, re.IGNORECASE) or re.search(r'\bwww\.[a-zA-Z0-9-]+\.[a-zA-Z]{2,}', temp_content, re.IGNORECASE):
        raise HTTPException(
            status_code=400,
            detail="Raw URLs are not allowed. / 帖子中不允许包含网址。"
        )


@router.post("/forum/posts", response_model=schemas.ForumPostResponse)
async def create_post(
    req: schemas.ForumPostCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    check_rate_limit(
        current_user.id,
        "post",
        5,
        "Daily limit of 5 posts exceeded. / 每日发帖次数已达上限（5个）。"
    )
    validate_post_content(req.content, req.category)

    # Move temp images to active in S3 and rewrite URLs
    content, image = confirm_temp_images(req.content, req.image)

    tags = []
    if req.body_type:
        tags.append(req.body_type)
    if req.multi_color_type:
        tags.append(req.multi_color_type)
    if not tags:
        tags = ["Custom", "Figure"]

    post = models.ForumPost(
        title=req.title,
        content=content,
        category=req.category,
        image=image,
        tags=tags,
        user_id=current_user.id,
        likes_count=0,
        views_count=1,
        body_type=req.body_type,
        multi_color_type=req.multi_color_type
    )
    db.add(post)
    db.commit()
    db.refresh(post)

    role = "Member"
    if current_user.is_admin:
        role = "Mod"
    elif current_user.is_pro:
        role = "Pro Member"

    return schemas.ForumPostResponse(
        id=post.id,
        title=post.title,
        content=post.content,
        category=post.category,
        image=post.image,
        tags=post.tags,
        author=current_user.username or "GuestMaker",
        authorAvatar=current_user.picture,
        authorMinecraftSkinUrl=current_user.minecraft_skin_url,
        isPro=current_user.is_pro,
        role=role,
        likes=0,
        views=1,
        isLiked=False,
        printSettings=schemas.PrintSettings(),
        comments=[],
        commentsCount=0,
        createdAt=get_relative_time(post.created_at),
        bodyType=post.body_type,
        multiColorType=post.multi_color_type
    )


@router.post("/forum/posts/{post_id}/like")
async def like_post(
    post_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    post = db.query(models.ForumPost).filter(models.ForumPost.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    existing_like = db.query(models.ForumPostLike).filter(
        models.ForumPostLike.user_id == current_user.id,
        models.ForumPostLike.post_id == post.id
    ).first()

    if existing_like:
        db.delete(existing_like)
        post.likes_count = max(0, post.likes_count - 1)
        db.commit()
        db.refresh(post)
        
        # Remove unread like notification if it exists
        notif = db.query(models.ForumNotification).filter(
            models.ForumNotification.user_id == post.user_id,
            models.ForumNotification.sender_id == current_user.id,
            models.ForumNotification.type == "like",
            models.ForumNotification.post_id == post.id
        ).first()
        if notif:
            db.delete(notif)
            db.commit()

        return {"isLiked": False, "likes": post.likes_count}
    else:
        like = models.ForumPostLike(user_id=current_user.id, post_id=post.id)
        db.add(like)
        post.likes_count += 1
        db.commit()
        db.refresh(post)

        if post.user_id != current_user.id:
            notif = models.ForumNotification(
                user_id=post.user_id,
                sender_id=current_user.id,
                type="like",
                post_id=post.id,
                is_read=False
            )
            db.add(notif)
            db.commit()

        return {"isLiked": True, "likes": post.likes_count}


@router.post("/forum/posts/{post_id}/comments", response_model=schemas.ForumCommentResponse)
async def create_comment(
    post_id: str,
    req: schemas.ForumCommentCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    check_rate_limit(
        current_user.id,
        "comment",
        50,
        "Daily limit of 50 comments/replies exceeded. / 每日回复次数已达上限（50次）。"
    )
    post = db.query(models.ForumPost).filter(models.ForumPost.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    if req.parent_id:
        parent_comment = db.query(models.ForumComment).filter(
            models.ForumComment.id == req.parent_id,
            models.ForumComment.post_id == post_id
        ).first()
        if not parent_comment:
            raise HTTPException(status_code=400, detail="Parent comment not found")

    comment = models.ForumComment(
        post_id=post_id,
        parent_id=req.parent_id,
        user_id=current_user.id,
        content=req.content
    )
    db.add(comment)
    db.commit()
    db.refresh(comment)

    if req.parent_id:
        parent_comment = db.query(models.ForumComment).filter(models.ForumComment.id == req.parent_id).first()
        if parent_comment and parent_comment.user_id != current_user.id:
            notif = models.ForumNotification(
                user_id=parent_comment.user_id,
                sender_id=current_user.id,
                type="reply",
                post_id=post_id,
                comment_id=comment.id,
                is_read=False
            )
            db.add(notif)
            db.commit()
    else:
        if post.user_id != current_user.id:
            notif = models.ForumNotification(
                user_id=post.user_id,
                sender_id=current_user.id,
                type="comment",
                post_id=post_id,
                comment_id=comment.id,
                is_read=False
            )
            db.add(notif)
            db.commit()

    return schemas.ForumCommentResponse(
        id=comment.id,
        author=current_user.username or "GuestMaker",
        avatarUrl=current_user.picture,
        minecraftSkinUrl=current_user.minecraft_skin_url,
        isPro=current_user.is_pro,
        content=comment.content,
        createdAt=get_relative_time(comment.created_at),
        replies=[]
    )


@router.get("/forum/notifications", response_model=schemas.ForumNotificationsPaginatedResponse)
async def list_notifications(
    page: int = 1,
    page_size: int = 10,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    check_rate_limit(
        current_user.id,
        "notifications",
        60,
        "Limit of 60 notification checks per minute exceeded. / 每分钟获取通知列表次数已达上限（60次）。",
        period=60
    )
    query = db.query(models.ForumNotification).filter(
        models.ForumNotification.user_id == current_user.id
    )
    total = query.count()
    unread_count = query.filter(models.ForumNotification.is_read == False).count()

    offset = (page - 1) * page_size
    notifs = query.order_by(desc(models.ForumNotification.created_at)).offset(offset).limit(page_size).all()

    sender_ids = {n.sender_id for n in notifs if n.sender_id}
    senders = db.query(models.User).filter(models.User.id.in_(sender_ids)).all() if sender_ids else []
    sender_map = {s.id: s for s in senders}

    post_ids = {n.post_id for n in notifs if n.post_id and n.type not in ("daily_login", "monthly_login", "subscription_grant", "system_gift")}
    posts = db.query(models.ForumPost).filter(models.ForumPost.id.in_(post_ids)).all() if post_ids else []
    post_map = {p.id: p for p in posts}

    # Load CreditLog sources for system gifts to show the gift message in mailbox
    gift_log_ids = {n.post_id for n in notifs if n.type == "system_gift" and n.post_id}
    gift_log_map = {}
    if gift_log_ids:
        gift_logs = db.query(models.CreditLog).filter(models.CreditLog.id.in_(gift_log_ids)).all()
        gift_log_map = {g.id: g.source for g in gift_logs}

    res = []
    for n in notifs:
        sender = sender_map.get(n.sender_id)
        post = post_map.get(n.post_id) if n.type not in ("daily_login", "monthly_login", "subscription_grant", "system_gift") else None
        
        if n.type in ("daily_login", "monthly_login", "subscription_grant"):
            sender_name = "System"
            post_title = n.comment_id or "0"
        elif n.type == "system_gift":
            sender_name = "System"
            gift_msg = gift_log_map.get(n.post_id)
            post_title = f"{n.comment_id or '0'}|{gift_msg or 'Gift'}"
        else:
            sender_name = sender.username if sender else "Anonymous"
            post_title = post.title if post else "Deleted Post"
            
        res.append(schemas.ForumNotificationResponse(
            id=n.id,
            type=n.type,
            senderName=sender_name,
            senderAvatar=sender.picture if sender else None,
            senderMinecraftSkinUrl=sender.minecraft_skin_url if sender else None,
            postId=n.post_id,
            postTitle=post_title,
            isRead=n.is_read,
            createdAt=get_relative_time(n.created_at)
        ))
    return schemas.ForumNotificationsPaginatedResponse(
        notifications=res,
        total=total,
        page=page,
        page_size=page_size,
        unread_count=unread_count
    )


@router.post("/forum/notifications/read-all")
async def read_all_notifications(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    db.query(models.ForumNotification).filter(
        models.ForumNotification.user_id == current_user.id,
        models.ForumNotification.is_read == False
    ).update({"is_read": True}, synchronize_session=False)
    db.commit()
    return {"status": "success", "message": "All notifications marked as read"}


@router.post("/forum/notifications/{notif_id}/read")
async def read_notification(
    notif_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    notif = db.query(models.ForumNotification).filter(
        models.ForumNotification.id == notif_id,
        models.ForumNotification.user_id == current_user.id
    ).first()
    if not notif:
        raise HTTPException(status_code=404, detail="Notification not found")
    
    notif.is_read = True
    db.commit()
    return {"status": "success", "message": "Notification marked as read"}


def extract_youtube_id(url: str) -> Optional[str]:
    import re
    if not url:
        return None
    url = url.strip()
    # 11-char ID
    if len(url) == 11 and re.match(r'^[a-zA-Z0-9_-]+$', url):
        return url
    patterns = [
        r'(?:v=|\/v\/|embed\/|shorts\/|youtu\.be\/|\/embed\/|\/watch\?v=|\/watch\?.+&v=)([^#\&\?]{11})'
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


@router.get("/forum/videos", response_model=List[schemas.ForumVideoResponse])
async def list_videos(db: Session = Depends(get_db)):
    videos = db.query(models.ForumVideo).order_by(models.ForumVideo.created_at.desc()).all()

    res = []
    for v in videos:
        res.append(schemas.ForumVideoResponse(
            id=v.id,
            youtubeId=v.youtube_id
        ))
    return res


@router.post("/forum/videos", response_model=schemas.ForumVideoResponse)
async def create_video(
    req: schemas.ForumVideoCreate,
    db: Session = Depends(get_db),
    current_admin: models.User = Depends(auth.get_current_admin)
):
    youtube_id = extract_youtube_id(req.youtube_url)
    if not youtube_id:
        raise HTTPException(status_code=400, detail="Invalid YouTube URL or ID.")
        
    video = models.ForumVideo(
        youtube_id=youtube_id
    )
    db.add(video)
    db.commit()
    db.refresh(video)
    
    return schemas.ForumVideoResponse(
        id=video.id,
        youtubeId=video.youtube_id
    )


@router.delete("/forum/videos/{video_id}")
async def delete_video(
    video_id: str,
    db: Session = Depends(get_db),
    current_admin: models.User = Depends(auth.get_current_admin)
):
    video = db.query(models.ForumVideo).filter(models.ForumVideo.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
        
    db.delete(video)
    db.commit()
    return {"status": "success"}


@router.delete("/forum/posts/{post_id}")
async def delete_post(
    post_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    post = db.query(models.ForumPost).filter(models.ForumPost.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
        
    # Check authorization: user is the author OR is an admin
    if post.user_id != current_user.id and not current_user.is_admin:
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to delete this post. / 您没有权限删除此帖子。"
        )
        
    # Delete post likes, comments, notifications, and the post itself
    db.query(models.ForumPostLike).filter(models.ForumPostLike.post_id == post_id).delete(synchronize_session=False)
    db.query(models.ForumComment).filter(models.ForumComment.post_id == post_id).delete(synchronize_session=False)
    db.query(models.ForumNotification).filter(models.ForumNotification.post_id == post_id).delete(synchronize_session=False)
    db.delete(post)
    db.commit()
    
    return {"status": "success", "message": "Post deleted successfully"}


@router.patch("/forum/posts/{post_id}", response_model=schemas.ForumPostResponse)
async def update_post(
    post_id: str,
    req: schemas.ForumPostUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    post = db.query(models.ForumPost).filter(models.ForumPost.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
        
    # Check authorization: user is the author OR is an admin
    if post.user_id != current_user.id and not current_user.is_admin:
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to update this post. / 您没有权限修改此帖子。"
        )
        
    # Validation if switching to showcase: Showcase posts must contain at least one image
    if req.category:
        if req.category == 'showcase':
            validate_post_content(post.content, req.category)
        post.category = req.category

    if req.title and req.title.strip():
        post.title = req.title.strip()
            
    db.commit()
    db.refresh(post)
    
    # Return post details
    author = db.query(models.User).filter(models.User.id == post.user_id).first()
    role = "Member"
    if author:
        if author.is_admin:
            role = "Mod"
        elif author.is_pro:
            role = "Pro Member"

    comments_count = db.query(models.ForumComment).filter(models.ForumComment.post_id == post.id).count()
    is_liked = db.query(models.ForumPostLike).filter(models.ForumPostLike.user_id == current_user.id, models.ForumPostLike.post_id == post.id).first() is not None

    return schemas.ForumPostResponse(
        id=post.id,
        title=post.title,
        content=post.content,
        category=post.category,
        image=post.image,
        tags=post.tags or [],
        author=author.username if author else "GuestMaker",
        authorAvatar=author.picture if author else None,
        authorMinecraftSkinUrl=author.minecraft_skin_url if author else None,
        isPro=author.is_pro if author else False,
        role=role,
        likes=post.likes_count,
        views=post.views_count,
        isLiked=is_liked,
        printSettings=schemas.PrintSettings(),
        comments=[],
        commentsCount=comments_count,
        createdAt=get_relative_time(post.created_at),
        bodyType=post.body_type,
        multiColorType=post.multi_color_type
    )

