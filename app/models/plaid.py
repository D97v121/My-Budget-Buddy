from datetime import datetime
from app import db
from app.encryption_utils import encrypt_data, decrypt_data
from cryptography.fernet import InvalidToken
from sqlalchemy import DateTime

class PlaidItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    item_id = db.Column(db.String(255), nullable=False)
    access_token = db.Column(db.String(255), nullable=False, server_default='')
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    cursor = db.Column(db.String, nullable=True)
    institution_id = db.Column(db.String, nullable=True)  # Add this line
    institution_name = db.Column(db.String, nullable=True) 


    # Relationship with User model
    user = db.relationship('User', backref=db.backref('plaid_items', lazy=True))

    # Securely store access_token as encrypted but allow decrypted access
    @property
    def decrypted_access_token(self):
        try:
            return decrypt_data(self.access_token)
        except InvalidToken:
            return self.access_token
        
    @decrypted_access_token.setter
    def decrypted_access_token(self, token):
        """Encrypt access_token before storing"""
        self.access_token = encrypt_data(token)  # ✅ Modify the column directly