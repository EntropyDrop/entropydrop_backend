from fastapi import APIRouter, Depends, HTTPException, File, UploadFile, Form, Query
from typing import List, Optional
from sqlalchemy.orm import Session
import uuid
from database import get_db
import models
import schemas
import auth
from s3_utils import get_cdn_url, generate_presigned_url_get, get_s3_url
import backend_utils
from config import settings

router = APIRouter(prefix="/api", tags=["collections"])

def resolve_item_urls(item, col_is_public, db):
    item_data = dict(item.data) if item.data else {}
    if item.log_id:
        log = db.query(models.GenerationLog).filter(models.GenerationLog.id == item.log_id).first()
        if log:
            # Prefer log.result_url which uses log.is_public
            url = log.result_url
            item_data["result"] = url
            item_data["url"] = url
    else:
        for key in ["result", "url", "preview", "result_render_2d"]:
            if key in item_data and isinstance(item_data[key], str) and not item_data[key].startswith("http"):
                item_data[key] = get_s3_url(item_data[key], col_is_public)
    return item_data


@router.get("/collections", response_model=schemas.PaginatedCollections)
async def get_collections(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    is_public: Optional[bool] = None,
    show_original_creation: bool = True,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    virtual_collections = []

    if page == 1 and show_original_creation:
        # 1. Calculate Virtual Collection Counts
        # Liked Items
        liked_count = db.query(models.GenerationLog).join(
            models.UserLike, models.GenerationLog.id == models.UserLike.log_id
        ).filter(
            models.UserLike.user_id == current_user.id,
            models.GenerationLog.is_deleted == False,
            models.GenerationLog.status == "success"
        ).count()

        # Public Creations
        pub_count = db.query(models.GenerationLog).filter(
            models.GenerationLog.user_id == current_user.id,
            models.GenerationLog.is_public == True,
            models.GenerationLog.is_deleted == False,
            models.GenerationLog.status == "success"
        ).count()

        # Private Creations
        priv_count = db.query(models.GenerationLog).filter(
            models.GenerationLog.user_id == current_user.id,
            models.GenerationLog.is_public == False,
            models.GenerationLog.is_deleted == False,
            models.GenerationLog.status == "success"
        ).count()


        # Fetch Previews (first 3 items for each)
        def get_log_previews(query):
            logs = query.order_by(models.GenerationLog.created_at.desc()).limit(3).all()
            return [{"id": l.id, "data": {"result": l.result_url}} for l in logs]

        liked_previews = get_log_previews(db.query(models.GenerationLog).join(
            models.UserLike, models.GenerationLog.id == models.UserLike.log_id
        ).filter(
            models.UserLike.user_id == current_user.id,
            models.GenerationLog.is_deleted == False,
            models.GenerationLog.status == "success"
        ))

        pub_previews = get_log_previews(db.query(models.GenerationLog).filter(
            models.GenerationLog.user_id == current_user.id,
            models.GenerationLog.is_public == True,
            models.GenerationLog.is_deleted == False,
            models.GenerationLog.status == "success"
        ))

        priv_previews = get_log_previews(db.query(models.GenerationLog).filter(
            models.GenerationLog.user_id == current_user.id,
            models.GenerationLog.is_public == False,
            models.GenerationLog.is_deleted == False,
            models.GenerationLog.status == "success"
        ))

        virtual_collections = [
            {
                "id": "liked",
                "name": "My Likes",
                "user_id": current_user.id,
                "is_public": False,
                "item_count": liked_count,
                "original_creation": True,
                "previews": liked_previews
            },
            {
                "id": "creations_public",
                "name": "My Creations (Public)",
                "user_id": current_user.id,
                "is_public": True,
                "item_count": pub_count,
                "original_creation": True,
                "previews": pub_previews
            },
            {
                "id": "creations_private",
                "name": "My Creations (Private)",
                "user_id": current_user.id,
                "is_public": False,
                "item_count": priv_count,
                "original_creation": True,
                "previews": priv_previews
            }
        ]

    query = db.query(models.Collection).filter(models.Collection.user_id == current_user.id)
    if is_public is not None:
        query = query.filter(models.Collection.is_public == is_public)
    
    total_custom = query.count()
    skip = (page - 1) * page_size
    collections = query.offset(skip).limit(page_size).all()
    
    results = []

    # Count items and build list
    for col in collections:
        count = db.query(models.CollectionItem).filter(models.CollectionItem.collection_id == col.id).count()
        previews_items = db.query(models.CollectionItem).filter(
            models.CollectionItem.collection_id == col.id
        ).order_by(models.CollectionItem.created_at.desc()).limit(3).all()
        
        previews = []
        for i in previews_items:
            item_data = resolve_item_urls(i, col.is_public, db)
            previews.append({"id": i.id, "data": item_data})
        
        results.append({
            "id": col.id,
            "name": col.name,
            "user_id": col.user_id,
            "is_public": col.is_public,
            "item_count": count,
            "original_creation": False,
            "previews": previews
        })
        
    return backend_utils.paginate_response(results, total_custom, page, page_size, original_items=virtual_collections)

@router.post("/collections", response_model=schemas.CollectionResponse)
async def create_collection(req: schemas.CollectionCreate, db: Session = Depends(get_db), current_user: models.User = Depends(auth.get_current_user)):
    """Create new collection"""
    # Collection Count Limit Check
    count = db.query(models.Collection).filter(
        models.Collection.user_id == current_user.id,
        models.Collection.is_public == req.is_public
    ).count()
    
    if req.is_public:
        if count >= 100:
             raise HTTPException(status_code=400, detail="Public collection limit reached (100 collections)")
    else:
        if not current_user.is_pro_active:
             raise HTTPException(status_code=403, detail="Free users have no private collection quota, please subscribe to Pro")
        if count >= 200:
             raise HTTPException(status_code=400, detail="Private collection limit reached (200 collections)")

    col = models.Collection(
        name=req.name,
        user_id=current_user.id,
        is_public=req.is_public
    )
    db.add(col)
    db.commit()
    db.refresh(col)

    return {
        "id": col.id,
        "name": col.name,
        "user_id": col.user_id,
        "is_public": col.is_public,
        "item_count": 0,
        "original_creation": False
    }

@router.delete("/collections/{id}")
async def delete_collection(id: str, db: Session = Depends(get_db), current_user: models.User = Depends(auth.get_current_user)):
    """Delete collection"""
    col = db.query(models.Collection).filter(models.Collection.id == id, models.Collection.user_id == current_user.id).first()
    if not col:
        raise HTTPException(status_code=404, detail="Collection not found")

    
    # Delete associated items
    db.query(models.CollectionItem).filter(models.CollectionItem.collection_id == id).delete()
    db.delete(col)
    db.commit()
    return {"message": "Collection deleted"}

@router.put("/collections/{id}")
async def update_collection(id: str, req: schemas.CollectionCreate, db: Session = Depends(get_db), current_user: models.User = Depends(auth.get_current_user)):
    """Update collection name"""
    col = db.query(models.Collection).filter(models.Collection.id == id, models.Collection.user_id == current_user.id).first()
    if not col:
        raise HTTPException(status_code=404, detail="Collection not found")

    col.name = req.name
        
    db.commit()
    db.refresh(col)
    return {
        "id": col.id,
        "name": col.name,
        "is_public": col.is_public
    }

@router.get("/collections/items", response_model=schemas.PaginatedCollectionItems)
async def get_collection_items(
    collection_id: str, 
    user_id: str, 
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    name: Optional[str] = None,
    mode: Optional[str] = None,
    db: Session = Depends(get_db), 
    current_user: models.User = Depends(auth.get_current_user)
):
    """Get all items in the collection"""
    if collection_id in ["liked", "creations_public", "creations_private"]:
        # For private virtual collections, always use authenticated user's ID
        if collection_id in ["liked", "creations_private"]:
            if not current_user:
                raise HTTPException(status_code=403, detail="Permission denied")
            target_id = current_user.id
        else:
            target_id = user_id

        skip = (page - 1) * page_size
        if collection_id == "liked":
            query = db.query(models.GenerationLog).join(
                models.UserLike, models.GenerationLog.id == models.UserLike.log_id
            ).filter(
                models.UserLike.user_id == target_id,
                models.GenerationLog.is_deleted == False,
                models.GenerationLog.status == "success"
            )
        elif collection_id == "creations_public":
            query = db.query(models.GenerationLog).filter(
                models.GenerationLog.user_id == target_id,
                models.GenerationLog.is_public == True,
                models.GenerationLog.is_deleted == False,
                models.GenerationLog.status == "success"
            )
        else:
            query = db.query(models.GenerationLog).filter(
                models.GenerationLog.user_id == target_id,
                models.GenerationLog.is_public == False,
                models.GenerationLog.is_deleted == False,
                models.GenerationLog.status == "success"
            )
 
        if name:
            safe_name = name.replace("%", "\\%").replace("_", "\\_")
            query = query.filter(models.GenerationLog.name.ilike(f"%{safe_name}%"))
        if mode:
            query = query.filter(models.GenerationLog.mode == mode)


        total = query.count()
        logs = query.order_by(models.GenerationLog.created_at.desc()).offset(skip).limit(page_size).all()

        processed_items = []
        for log in logs:
            item_data = {}
            item_data["result"] = log.result_url

            processed_items.append({
                "id": str(log.id),
                "collection_id": collection_id,
                "name": log.name or (log.prompt[:100] if log.prompt else "Untitled"),
                "type": log.mode or "unknown",
                "log_id": log.id,
                "data": item_data
            })

        return backend_utils.paginate_response(processed_items, total, page, page_size)
    col: Optional[models.Collection] = db.query(models.Collection).filter(models.Collection.id == collection_id).first()
    if not col:
        raise HTTPException(status_code=404, detail="Collection not found")
        
    if not col.is_public:
        if not current_user or col.user_id != current_user.id:
            raise HTTPException(status_code=403, detail="Permission denied")
            
    skip = (page - 1) * page_size
    from sqlalchemy import or_
    query = db.query(models.CollectionItem).join(
        models.GenerationLog, models.CollectionItem.log_id == models.GenerationLog.id, isouter=True
    ).filter(
        models.CollectionItem.collection_id == collection_id,
        or_(models.GenerationLog.is_deleted == False, models.CollectionItem.log_id.is_(None))
    )
 
    if name:
        safe_name = name.replace("%", "\\%").replace("_", "\\_")
        query = query.filter(models.GenerationLog.name.ilike(f"%{safe_name}%"))
    if mode:
        query = query.filter(models.GenerationLog.mode == mode)
    total = query.count()
    items = query.order_by(models.CollectionItem.created_at.desc()).offset(skip).limit(page_size).all()


    processed_items = []
    for item in items:
        item_data = resolve_item_urls(item, col.is_public, db)
        item_name = "Untitled"
        item_type = item.type # Fallback
        if item.log_id:
            log = db.query(models.GenerationLog).filter(models.GenerationLog.id == item.log_id).first()
            if log:
                item_name = log.name or (log.prompt[:100] if log.prompt else "Untitled")
                item_type = log.mode or item.type

        processed_item = {
            "id": item.id,
            "collection_id": item.collection_id,
            "name": item_name,
            "type": item_type,
            "log_id": item.log_id,
            "data": item_data
        }
                
        processed_items.append(processed_item)

    return backend_utils.paginate_response(processed_items, total, page, page_size)

@router.post("/collections/items", response_model=schemas.CollectionItemResponse)
async def add_item_to_collection(item: schemas.CollectionItemCreate, db: Session = Depends(get_db), current_user: models.User = Depends(auth.get_current_user)):
    """Add item to collection"""
    col = db.query(models.Collection).filter(models.Collection.id == item.collection_id, models.Collection.user_id == current_user.id).first()
    if not col:
        raise HTTPException(status_code=404, detail="Collection not found")
        
    # File Count Limit Check & Pro Validation
    if not col.is_public:
        if not current_user.is_pro_active:
             raise HTTPException(status_code=403, detail="Free users have no private file quota")
        # Check Total Private Files Limit
        total_private_files = db.query(models.GenerationLog).filter(
            models.GenerationLog.user_id == current_user.id,
            models.GenerationLog.is_public == False,
            models.GenerationLog.is_deleted == False
        ).count()

        if total_private_files >= 2000:
             raise HTTPException(status_code=400, detail="Total private assets limit reached (2000 items)")

    item_count = db.query(models.CollectionItem).filter(models.CollectionItem.collection_id == item.collection_id).count()
    if item_count >= 200:
        raise HTTPException(status_code=400, detail="Collection item limit reached (200 items)")

    if item.log_id:
        log = db.query(models.GenerationLog).filter(models.GenerationLog.id == item.log_id).first()
        if log:
            # Permission check: must be owner or public
            if log.user_id != current_user.id and not log.is_public:
                raise HTTPException(status_code=403, detail="Permission denied")
            if col.is_public and not log.is_public:
                raise HTTPException(status_code=400, detail="Private images cannot be added to public collections")
            if not log.name:
                log.name = item.name

    new_item = models.CollectionItem(
        collection_id=item.collection_id,
        type=item.type,
        log_id=item.log_id,
        data=item.data
    )
    db.add(new_item)
    db.commit()
    db.refresh(new_item)
    return {
        "id": new_item.id,
        "collection_id": new_item.collection_id,
        "name": item.name[:100] if item.name else "Untitled",
        "type": new_item.type,
        "log_id": new_item.log_id,
        "data": new_item.data
    }

@router.delete("/collections/items/{id}")
async def remove_item_from_collection(id: str, db: Session = Depends(get_db), current_user: models.User = Depends(auth.get_current_user)):
    """Remove item from collection"""
    item = db.query(models.CollectionItem).filter(models.CollectionItem.id == id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
        
    col = db.query(models.Collection).filter(models.Collection.id == item.collection_id, models.Collection.user_id == current_user.id).first()
    if not col:
        raise HTTPException(status_code=403, detail="Permission denied")
        
    db.delete(item)
    db.commit()
    return {"message": "Item removed"}

@router.post("/collections/items/{id}/move")
async def move_item(
    id: str, 
    req: schemas.ItemMoveRequest, 
    db: Session = Depends(get_db), 
    current_user: models.User = Depends(auth.get_current_user)
):
    """Move item to another collection"""
    item = db.query(models.CollectionItem).filter(models.CollectionItem.id == id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
        
    # Verify current collection ownership
    current_col = db.query(models.Collection).filter(models.Collection.id == item.collection_id, models.Collection.user_id == current_user.id).first()
    if not current_col:
        raise HTTPException(status_code=403, detail="Permission denied")
        
    # Verify target collection ownership
    target_col = db.query(models.Collection).filter(models.Collection.id == req.target_collection_id, models.Collection.user_id == current_user.id).first()
    if not target_col:
        raise HTTPException(status_code=404, detail="Target collection not found")
        
    # Privacy check: public images -> public collections only, private images -> private collections only
    skin_is_public = current_col.is_public
    if item.log_id:
        log = db.query(models.GenerationLog).filter(models.GenerationLog.id == item.log_id).first()
        if log:
            skin_is_public = log.is_public

    if skin_is_public != target_col.is_public:
        if skin_is_public:
            raise HTTPException(status_code=400, detail="Public images can only be moved to public collections")
        else:
            raise HTTPException(status_code=400, detail="Private images can only be moved to private collections")
            
    item.collection_id = req.target_collection_id
    db.commit()
    return {"message": "Item moved successfully"}

@router.post("/collections/{id}/upload", response_model=schemas.CollectionItemResponse)
async def upload_item_to_collection(
    id: str,
    file: UploadFile = File(...),
    name: Optional[str] = Form(None),
    mode: str = Form("human_upload"), # 'human_edit', 'human_upload'.
    parent: Optional[str] = Form(None), # parent log_id
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    if mode != "human_upload" and mode != "human_edit":
        raise HTTPException(status_code=400, detail="Invalid mode")

    """Upload image to collection and create generation log"""
    if not id in ["creations_public", "creations_private"]:
        raise HTTPException(status_code=400, detail="Custom collections do not support manual uploads")

    is_public = (id == "creations_public")
    
    if not is_public:
        if not current_user.is_pro_active:
             raise HTTPException(status_code=403, detail="Free users have no private quota, please subscribe to Pro")
        # Check total private
        total_private_files = db.query(models.GenerationLog).filter(
            models.GenerationLog.user_id == current_user.id,
            models.GenerationLog.is_public == False,
            models.GenerationLog.is_deleted == False
        ).count()

        if total_private_files >= 2000:
             raise HTTPException(status_code=400, detail="Total private assets limit reached (2000 items)")
        
    if parent and id == "creations_public":
        parent_log = db.query(models.GenerationLog).filter(models.GenerationLog.id == parent).first()
        if parent_log and not parent_log.is_public:
            raise HTTPException(status_code=400, detail="Private models cannot be saved as public")
            
    file_content = await file.read()
    if len(file_content) > 512 * 1024:
        raise HTTPException(status_code=400, detail="File too large (Max 512KB)")

    from PIL import Image
    import io
    try:
        img = Image.open(io.BytesIO(file_content))
        if img.size != (64, 64):
            raise HTTPException(status_code=400, detail="Invalid dimensions (Must be 64x64)")
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=400, detail="Invalid image file")

    s3id = uuid.uuid4().hex
    filename = f"collections/{s3id}.png"
    
    bucket = settings.AWS_BUCKET_NAME if is_public else settings.AWS_PRIVATE_BUCKET_NAME
    
    upload_args = {
        "Bucket": bucket,
        "Key": filename,
        "Body": file_content,
        "ContentType": file.content_type or "image/png"
    }
    
    if is_public:
        upload_args["ACL"] = 'public-read'
        
    from s3_utils import s3_client
    s3_client.put_object(**upload_args)
    
    # Create GenerationLog
    log = models.GenerationLog(
        prompt='',
        name=(name or (file.filename if file.filename else f"Upload {s3id[:8]}"))[:100],
        mode=mode,
        source=None,
        result=filename,
        user_id=current_user.id,
        is_public=is_public,
        parent=parent,
        status="success"
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    
    from s3_utils import get_cdn_url, generate_presigned_url_get
    if is_public:
        result_url = get_cdn_url(filename, bucket=bucket)
    else:
        result_url = generate_presigned_url_get(filename, bucket=bucket)
        
    return {
        "id": log.id,
        "collection_id": id,
        "name": log.name,
        "type": "image",
        "log_id": log.id,
        "data": {"result": result_url}
    }


@router.get("/logs/{log_id}/collections", response_model=List[str])
async def get_log_collections(log_id: str, db: Session = Depends(get_db), current_user: models.User = Depends(auth.get_current_user)):
    """Get list of collection IDs that the log belongs to"""
    items = db.query(models.CollectionItem).join(
        models.Collection, models.CollectionItem.collection_id == models.Collection.id
    ).filter(
        models.CollectionItem.log_id == log_id,
        models.Collection.user_id == current_user.id
    ).all()
    return [item.collection_id for item in items]

@router.post("/logs/{log_id}/collections")
async def update_log_collections(log_id: str, collection_ids: List[str], db: Session = Depends(get_db), current_user: models.User = Depends(auth.get_current_user)):
    """Update collections the log belongs to"""
    # Get log info (optional, used for name etc.)
    log = db.query(models.GenerationLog).filter(models.GenerationLog.id == log_id).first()
    if not log:
        raise HTTPException(status_code=404, detail="Log not found")
        
    # Permission check: must be owner or public
    if log.user_id != current_user.id and not log.is_public:
        raise HTTPException(status_code=403, detail="Permission denied")
        
    log_name = log.name if log and log.name else (log.prompt if log and log.prompt else f"Generation {log_id[:8]}")

    # Get current collection ownership
    current_items = db.query(models.CollectionItem).join(
        models.Collection, models.CollectionItem.collection_id == models.Collection.id
    ).filter(
        models.CollectionItem.log_id == log_id,
        models.Collection.user_id == current_user.id
    ).all()
    
    current_col_ids = {item.collection_id for item in current_items}
    target_col_ids = set(collection_ids)
    
    # Items to remove
    to_remove = [item for item in current_items if item.collection_id not in target_col_ids]
    for item in to_remove:
        db.delete(item)
        
    # Items to add
    to_add = target_col_ids - current_col_ids
    
    # Pre-check collection file limit
    for col_id in to_add:
        col = db.query(models.Collection).filter(models.Collection.id == col_id).first()
        if col:
            existing_count = db.query(models.CollectionItem).filter(models.CollectionItem.collection_id == col_id).count()
            if existing_count >= 200:
                raise HTTPException(status_code=400, detail=f"Collection '{col.name}' item limit reached (200 items)")

    for col_id in to_add:
        # Verify collection ownership
        col = db.query(models.Collection).filter(models.Collection.id == col_id, models.Collection.user_id == current_user.id).first()

        if not col:
            continue
            
        if col.is_public and not log.is_public:
            raise HTTPException(status_code=400, detail="Private images cannot be added to public collections")
            
        new_item = models.CollectionItem(
            collection_id=col_id,
            type="image",
            log_id=log_id,
            data={"id": log_id, "result": log.result if log else ""}
        )
        db.add(new_item)
        
    db.commit()
    return {"message": "Collections updated"}

@router.get("/logs/{log_id}/public_collections", response_model=schemas.PaginatedCollections)
async def get_log_public_collections(
    log_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: Optional[models.User] = Depends(auth.get_current_user_optional)
):
    """Get all public collections containing the specified log"""
    skip = (page - 1) * page_size
    query = db.query(models.Collection).join(
        models.CollectionItem, models.Collection.id == models.CollectionItem.collection_id
    ).filter(
        models.CollectionItem.log_id == log_id,
        models.Collection.is_public == True
    )
    total = query.count()
    collections = query.order_by(models.Collection.created_at.desc()).offset(skip).limit(page_size).all()
    
    results = []
    for col in collections:
        count = db.query(models.CollectionItem).filter(models.CollectionItem.collection_id == col.id).count()
        user = db.query(models.User).filter(models.User.id == col.user_id).first()
        previews_items = db.query(models.CollectionItem).filter(
            models.CollectionItem.collection_id == col.id
        ).order_by(models.CollectionItem.created_at.desc()).limit(3).all()
        
        previews = []
        for i in previews_items:
            item_data = resolve_item_urls(i, col.is_public, db)
            previews.append({"id": i.id, "data": item_data})
        
        results.append({
            "id": col.id,
            "name": col.name,
            "user_id": col.user_id,
            "is_public": col.is_public,
            "item_count": count,
            "username": user.username if user else "Unknown",
            "original_creation": False,
            "previews": previews
        })
        
    return backend_utils.paginate_response(results, total, page, page_size, original_items=[])

@router.get("/users/{user_id}/collections", response_model=schemas.PaginatedCollections)
async def get_user_public_collections(
    user_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: Optional[models.User] = Depends(auth.get_current_user_optional)
):
    """Get user's public collections"""

    pub_count = db.query(models.GenerationLog).filter(
        models.GenerationLog.user_id == user_id,
        models.GenerationLog.is_public == True,
        models.GenerationLog.is_deleted == False,
        models.GenerationLog.status == "success"
    ).count()


    # Fetch Previews for Virtual
    pub_previews = []
    if page == 1:
        logs = db.query(models.GenerationLog).filter(
            models.GenerationLog.user_id == user_id,
            models.GenerationLog.is_public == True,
            models.GenerationLog.is_deleted == False,
            models.GenerationLog.status == "success"
        ).order_by(models.GenerationLog.created_at.desc()).limit(3).all()
        pub_previews = [{"id": l.id, "data": {"result": l.result_url}} for l in logs]

    virtual_collections = [
        {
            "id": "creations_public",
            "name": "My Creations (Public)",
            "user_id": user_id,
            "is_public": True,
            "item_count": pub_count,
            "original_creation": True,
            "previews": pub_previews
        }
    ]
 
    skip = (page - 1) * page_size
    query = db.query(models.Collection).filter(
        models.Collection.user_id == user_id,
        models.Collection.is_public == True
    )
    total_custom = query.count()
    collections = query.order_by(models.Collection.created_at.desc()).offset(skip).limit(page_size).all()
    
    results = []
        
    for col in collections:
        count = db.query(models.CollectionItem).filter(models.CollectionItem.collection_id == col.id).count()
        previews_items = db.query(models.CollectionItem).filter(
            models.CollectionItem.collection_id == col.id
        ).order_by(models.CollectionItem.created_at.desc()).limit(3).all()
        
        previews = []
        for i in previews_items:
            item_data = resolve_item_urls(i, col.is_public, db)
            previews.append({"id": i.id, "data": item_data})
        
        results.append({
            "id": col.id,
            "name": col.name,
            "user_id": col.user_id,
            "is_public": col.is_public,
            "item_count": count,
            "original_creation": False,
            "previews": previews
        })
        
    return backend_utils.paginate_response(results, total_custom, page, page_size, original_items=virtual_collections if page == 1 else [])

