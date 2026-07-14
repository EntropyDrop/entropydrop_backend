import boto3
from botocore.exceptions import ClientError
from config import settings

s3_client = boto3.client(
    's3',
    aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
    aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
    region_name=settings.AWS_REGION,
    endpoint_url=f"https://s3.{settings.AWS_REGION}.amazonaws.com"
)

def generate_presigned_url_get(object_name, bucket=None, expiration=3600):
    """Generate a presigned URL to share an S3 object (GET)"""
    bucket = bucket or settings.AWS_BUCKET_NAME
    try:
        response = s3_client.generate_presigned_url('get_object',
                                                    Params={'Bucket': bucket,
                                                            'Key': object_name},
                                                    ExpiresIn=expiration)
    except ClientError as e:
        print(e)
        return None
    return response

def generate_presigned_url(object_name, bucket=None, expiration=3600):
    """Generate a presigned URL for uploading to S3 (PUT)"""
    bucket = bucket or settings.AWS_BUCKET_NAME
    try:
        response = s3_client.generate_presigned_url('put_object',
                                                    Params={'Bucket': bucket,
                                                            'Key': object_name},
                                                    ExpiresIn=expiration)
    except ClientError as e:
        print(e)
        return None
    return response

def get_cdn_url(object_name, bucket=None):
    bucket = bucket or settings.AWS_BUCKET_NAME
    if settings.AWS_CDN_DOMAIN:
        return f"https://{settings.AWS_CDN_DOMAIN}/{object_name}"
    return f"https://{bucket}.s3.{settings.AWS_REGION}.amazonaws.com/{object_name}"

def get_s3_url(key: str, is_public: bool):
    if not key:
        return None
    if key.startswith("http"):
        return key
    if is_public:
        return get_cdn_url(key, bucket=settings.AWS_BUCKET_NAME)
    return generate_presigned_url_get(key, bucket=settings.AWS_PRIVATE_BUCKET_NAME)

def delete_from_s3(key: str, is_public: bool):
    """
    Delete object from S3
    """
    if not key:
        return
    bucket = settings.AWS_BUCKET_NAME if is_public else settings.AWS_PRIVATE_BUCKET_NAME
    try:
        s3_client.delete_object(Bucket=bucket, Key=key)
    except ClientError as e:
        print(f"Error deleting {key} from S3: {e}")

def upload_to_s3(file_content: bytes, key: str, is_public: bool, content_type: str = "image/png") -> str:
    """
    Upload file to S3 and return the Key
    """
    bucket = settings.AWS_BUCKET_NAME if is_public else settings.AWS_PRIVATE_BUCKET_NAME
    upload_args = {
        "Bucket": bucket,
        "Key": key,
        "Body": file_content,
        "ContentType": content_type
    }
    if is_public:
        upload_args["ACL"] = 'public-read'
    
    s3_client.put_object(**upload_args)
    
    return key

def download_from_s3(key: str, is_public: bool) -> bytes:
    """
    Download file content from S3
    """
    bucket = settings.AWS_BUCKET_NAME if is_public else settings.AWS_PRIVATE_BUCKET_NAME
    response = s3_client.get_object(Bucket=bucket, Key=key)
    return response['Body'].read()
