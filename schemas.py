from pydantic import BaseModel, ConfigDict, Field, StrictBool, HttpUrl
from typing import Optional, List, Literal
from datetime import datetime

class GoogleAuthRequest(BaseModel):
    token: str

class UpdateUsernameRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=100)

class UpdateMinecraftSkinRequest(BaseModel):
    skin_url: Optional[str] = Field(None, min_length=1, max_length=500)
    skin_type: Optional[str] = Field("strong", max_length=20)
    model_config = ConfigDict(extra="forbid")

class PublicUserProfile(BaseModel):
    id: str
    username: Optional[str] = None
    picture: Optional[str] = None
    skin_url: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class UserResponse(BaseModel):
    id: str
    email: str
    username: Optional[str] = None
    picture: Optional[str] = None
    google_id: Optional[str] = None
    skin_url: Optional[str] = None
    skin_type: Optional[str] = "strong"
    
    # Priority fields
    terms_agreed: Optional[bool] = False
    figure_print_terms_version: Optional[str] = None
    figure_print_terms_accepted_at: Optional[datetime] = None
    is_pro: bool = False
    is_admin: bool = False
    pro_expires_at: Optional[datetime] = None
    pro_level: str = "free"
    credits: int = 0
    
    # Quota fields
    text_to_skin_enabled: Optional[bool] = True
    image_to_skin_enabled: Optional[bool] = True
    image_edit_to_skin_enabled: Optional[bool] = True

    paypal_subscription_id: Optional[str] = None
    paypal_subscription_status: Optional[str] = None
    
    model_config = ConfigDict(from_attributes=True)

class FigurePrintTermsRequest(BaseModel):
    version: str = Field(..., min_length=1, max_length=32)
    accepted: StrictBool
    model_config = ConfigDict(extra="forbid")

class FigurePrintTermsResponse(BaseModel):
    required_version: str
    effective_date: str
    accepted_version: Optional[str] = None
    accepted_at: Optional[datetime] = None

class TokenResponse(BaseModel):
    access_token: str
    token_type: str
    expires_in_seconds: int
    user: UserResponse


class AccessTokenResponse(BaseModel):
    access_token: str
    token_type: str
    expires_in_seconds: int

class CollectionCreate(BaseModel):
    name: str = Field(..., max_length=100)
    is_public: Optional[bool] = True

class CollectionResponse(BaseModel):
    id: str
    name: str
    user_id: str
    is_public: bool
    item_count: Optional[int] = 0
    username: Optional[str] = None
    original_creation: Optional[bool] = False
    previews: Optional[List[dict]] = []

    model_config = ConfigDict(from_attributes=True)


class CollectionItemCreate(BaseModel):
    collection_id: str
    name: str = Field(..., max_length=100)
    type: str
    log_id: Optional[str] = None
    data: dict

class ItemMoveRequest(BaseModel):
    target_collection_id: str

class CollectionItemResponse(BaseModel):
    id: str
    collection_id: str
    name: str
    type: str
    log_id: Optional[str] = None
    data: dict

    model_config = ConfigDict(from_attributes=True)

class PaginatedCollectionItems(BaseModel):
    items: list[CollectionItemResponse]
    total: int
    page: int
    page_size: int
    total_pages: int

class PaginatedCollections(BaseModel):
    items: list[CollectionResponse]
    original_items: list[CollectionResponse] = []
    total: int
    page: int
    page_size: int
    total_pages: int

class LogNameUpdateRequest(BaseModel):
    name: str = Field(..., max_length=100)

class ShippingAddressBase(BaseModel):
    # Empty is retained only for reading historical addresses and order snapshots.
    recipient_name: str = Field("", max_length=300)
    country: str = Field(..., max_length=100)
    phone: str = Field(..., max_length=50)
    zip_code: str = Field(..., max_length=20)
    state: str = Field(..., max_length=100)
    city: str = Field(..., max_length=100)
    detail_address: str = Field(..., max_length=1000)
    is_default: Optional[bool] = False

class ShippingAddressCreate(ShippingAddressBase):
    pass

class ShippingAddressUpdate(BaseModel):
    recipient_name: Optional[str] = Field(None, max_length=300)
    country: Optional[str] = Field(None, max_length=100)
    phone: Optional[str] = Field(None, max_length=50)
    zip_code: Optional[str] = Field(None, max_length=20)
    state: Optional[str] = Field(None, max_length=100)
    city: Optional[str] = Field(None, max_length=100)
    detail_address: Optional[str] = Field(None, max_length=1000)
    is_default: Optional[bool] = None

class ShippingAddressResponse(ShippingAddressBase):
    id: str
    user_id: str

    model_config = ConfigDict(from_attributes=True)

from datetime import datetime

class OrderBase(BaseModel):
    address_id: Optional[str] = None
    order_type: str = Field("print", max_length=20) # 'print', 'subscription'
    model_config = ConfigDict(protected_namespaces=())

class OrderCreate(OrderBase):
    sticker_language: Literal["en", "zh-hans"] = "en"
    quantity: int = Field(1, ge=1, le=10, strict=True)
    log_id: Optional[str] = None # Used for direct link/Pro ordering
    model_type: Optional[str] = Field("PLA+sticker", max_length=100) # Default for direct links

class KitMaterial(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    quantity: int = Field(ge=1, le=1000, strict=True)
    description: Optional[str] = Field(None, max_length=1000)

class KitSpecifications(BaseModel):
    product_name: str = Field(min_length=1, max_length=200)
    dimensions: str = Field(min_length=1, max_length=200)
    materials: List[KitMaterial] = Field(min_length=1, max_length=100)
    assembly_note: str = Field(min_length=1, max_length=2000)

class ModelStockResponse(BaseModel):
    model_type: str
    available: bool
    stock: int
    price: float
    kit_specifications: Optional[KitSpecifications] = None

class StickerLabels(BaseModel):
    publisher: str
    user_id: str
    source: str

class OrderStickerSnapshot(BaseModel):
    schema_version: Literal[1] = 1
    origin: Literal["order", "legacy_backfill"]
    brand: str
    model_name: str
    skin_id: str
    skin_name: str
    publisher_id: str
    publisher_name: str
    source_url: str
    labels: StickerLabels
    missing_fields: List[str] = []

class OrderItemResponse(BaseModel):
    sticker_snapshot: Optional[OrderStickerSnapshot] = None
    kit_specifications_snapshot: Optional[KitSpecifications] = None
    kit_specifications_current: Optional[KitSpecifications] = None
    refer_log_id: Optional[str] = None
    id: str
    order_id: str
    skin_url: Optional[str] = None
    model_type: str
    price: float
    created_at: datetime

    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

class OrderResponse(OrderBase):
    id: str
    user_id: str
    status: str
    price: float
    shipping_fee: float
    total_price: float
    created_at: datetime
    paid_at: Optional[datetime] = None
    items: List[OrderItemResponse] = []
    address: Optional[ShippingAddressResponse] = None
    paypal_order_id: Optional[str] = None
    goods_status: Optional[str] = None
    figure_review_status: Optional[str] = None
    figure_review_reason: Optional[str] = None
    figure_reviewed_at: Optional[datetime] = None
    refund_status: Optional[str] = None
    tracking_number: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)

class PayRequest(BaseModel):
    paypal_order_id: Optional[str] = None

class OrderCheckoutRequest(BaseModel):
    return_url: HttpUrl

class SubscriptionCreateRequest(BaseModel):
    tier: str
    return_url: str

class PaginatedOrders(BaseModel):
    items: List[OrderResponse]
    total: int
    page: int
    page_size: int
    total_pages: int


class FeedbackCreate(BaseModel):
    is_good: bool


# Figure Forum schemas
class ForumPostCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=100)
    content: str = Field(..., min_length=1, max_length=10000)
    category: str = Field("discussions", max_length=50) # 'discussions' or 'showcase'
    body_type: Optional[str] = Field(None, max_length=50)
    multi_color_type: Optional[str] = Field(None, max_length=50)
    image: Optional[str] = Field(None, max_length=500)

class ForumPostUpdate(BaseModel):
    category: Optional[str] = Field(None, max_length=50)
    title: Optional[str] = Field(None, min_length=1, max_length=100)

class PrintSettings(BaseModel):
    printer: str = ""
    layerHeight: str = ""
    infill: str = ""
    printTime: str = ""
    material: str = ""

class ForumCommentCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=2000)
    parent_id: Optional[str] = None

class ForumCommentResponse(BaseModel):
    id: str
    author: str
    avatarUrl: Optional[str] = None
    skinUrl: Optional[str] = None
    isPro: bool = False
    content: str
    createdAt: str
    replies: List["ForumCommentResponse"] = []

    model_config = ConfigDict(from_attributes=True)

class ForumPostResponse(BaseModel):
    id: str
    title: str
    content: str
    category: str
    image: Optional[str] = None
    tags: List[str] = []
    author: str
    authorAvatar: Optional[str] = None
    authorSkinUrl: Optional[str] = None
    isPro: bool = False
    role: Optional[str] = None
    likes: int = 0
    views: int = 0
    isLiked: bool = False
    printSettings: PrintSettings
    comments: List[ForumCommentResponse] = []
    commentsCount: int = 0
    createdAt: str
    bodyType: Optional[str] = None
    multiColorType: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)

class ForumNotificationResponse(BaseModel):
    id: str
    type: str # 'like', 'comment', 'reply'
    senderName: str
    senderAvatar: Optional[str] = None
    senderSkinUrl: Optional[str] = None
    postId: Optional[str] = None
    postTitle: Optional[str] = None
    orderId: Optional[str] = None
    message: Optional[str] = None
    isRead: bool = False
    createdAt: str

    model_config = ConfigDict(from_attributes=True)

class ForumPostsPaginatedResponse(BaseModel):
    posts: List[ForumPostResponse]
    total: int
    page: int
    page_size: int

class ForumCommentsPaginatedResponse(BaseModel):
    comments: List[ForumCommentResponse]
    total: int
    page: int
    page_size: int

class ForumNotificationsPaginatedResponse(BaseModel):
    notifications: List[ForumNotificationResponse]
    total: int
    page: int
    page_size: int
    unread_count: int


class ForumVideoCreate(BaseModel):
    youtube_url: str = Field(..., min_length=1)


class ForumVideoResponse(BaseModel):
    id: str
    youtubeId: str

    model_config = ConfigDict(from_attributes=True)
