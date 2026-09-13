import secrets

def generate_base58_id(length=16):
    """Generate a 16-character Base58 random ID"""
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    return "".join(secrets.choice(alphabet) for _ in range(length))
