from pydantic import BaseModel, ConfigDict, Field
from typing import Optional, List
from datetime import datetime

class GoogleAuthRequest(BaseModel):
    token: str

class UpdateUsernameRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=100)

class UpdateMinecraftSkinRequest(BaseModel):
    minecraft_skin_url: Optional[str] = Field(None, min_length=1, max_length=500)

class UserResponse(BaseModel):
    id: str
    email: str
    username: Optional[str] = None
    picture: Optional[str] = None
    google_id: Optional[str] = None
    minecraft_skin_url: Optional[str] = None
    
    # Priority fields
    terms_agreed: Optional[bool] = False
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

class TokenResponse(BaseModel):
    access_token: str
    token_type: str
    user: UserResponse

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
    log_id: Optional[str] = None # Used for direct link/Pro ordering
    model_type: Optional[str] = Field("PLA+sticker", max_length=100) # Default for direct links

class OrderItemResponse(BaseModel):
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

    model_config = ConfigDict(from_attributes=True)

class PayRequest(BaseModel):
    paypal_order_id: Optional[str] = None

# Cart related models
class CartItemBase(BaseModel):
    log_id: str
    model_config = ConfigDict(protected_namespaces=())
    model_type: str = Field("PLA+sticker", max_length=100)

class CartItemCreate(CartItemBase):
    pass

class CartItemResponse(CartItemBase):
    id: str
    user_id: str
    created_at: datetime
    
    # Enriched for frontend use
    log_name: Optional[str] = None
    log_result: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)
        
class PaginatedCartItems(BaseModel):
    items: List[CartItemResponse]
    total: int
    page: int
    page_size: int
    total_pages: int

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
    minecraftSkinUrl: Optional[str] = None
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
    authorMinecraftSkinUrl: Optional[str] = None
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
    senderMinecraftSkinUrl: Optional[str] = None
    postId: Optional[str] = None
    postTitle: Optional[str] = None
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
