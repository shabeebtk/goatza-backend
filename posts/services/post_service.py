from django.db import transaction
from services.storage.factory import get_storage_service
from posts.models import Post
    
class PostService:

    @staticmethod
    def delete_post(post_id, actor):
        storage = get_storage_service()

        # get post
        if actor.is_user:
            post = Post.objects.filter(
                id=post_id,
                author_user=actor.user
            ).first()

        elif actor.is_org:
            post = Post.objects.filter(
                id=post_id,
                author_org=actor.organization
            ).first()
        else:
            return False, None

        if not post:
            return False, None

        try:
            with transaction.atomic():

                #  get first media to extract folder
                first_media = post.media.first()

                if first_media and first_media.public_id:
                    folder_path = "/".join(first_media.public_id.split("/")[:-1])

                    try:
                        storage.delete_folder_data(folder_path)
                    except Exception as e:
                        print(f"Folder delete failed: {e}")

                #  delete DB
                deleted_count, _ = post.delete()

                return deleted_count > 0, None

        except Exception as e:
            print(f"Delete post failed: {e}")
            return False, None