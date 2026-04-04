class BaseStorageService:
    def get_upload_config(self, user, upload_type: str):
        raise NotImplementedError

    def delete_file(self, public_id: str):
        raise NotImplementedError