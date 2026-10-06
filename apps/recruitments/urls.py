from django.urls import path
from apps.recruitments.views.recruitment_views import (
    CreateRecruitmentAPIView, ListRecruitmentsAPIView, RecruitmentDetailAPIView,
    UpdateRecruitmentAPIView, ChangeRecruitmentStatusAPIView,
    DiscoverRecruitmentsAPIView
)
from apps.recruitments.views.save_views import (
    ToggleSaveRecruitmentAPIView, SavedRecruitmentsListAPIView
)
from apps.recruitments.views.announcement_views import (
    RecruitmentAnnouncementsAPIView,
    AnnouncementRecipientsCountAPIView,
    AnnouncementDetailAPIView,
)
from apps.recruitments.views.application_views import (
    ApplyRecruitmentAPIView,
    ListRecruitmentApplicationsAPIView,
    RecruitmentApplicationDetailAPIView,
    WithdrawApplicationAPIView,
    BulkApplicationStatusAPIView,
    ApplicationStatusAPIView,
    MyApplicationsAPIView,
    ApplicationFeeAPIView,
    BulkApplicationFeeAPIView,
    MessageApplicantsAPIView,
    TrialPassAPIView,
    TrialFeedbackAPIView,
)

# base endpoint - "/recruitments"

urlpatterns = [
    path('create', CreateRecruitmentAPIView.as_view()),
    path('list', ListRecruitmentsAPIView.as_view()),
    path('discover', DiscoverRecruitmentsAPIView.as_view()),
    path('<uuid:recruitment_id>/details', RecruitmentDetailAPIView.as_view()),
    path('<uuid:recruitment_id>/update', UpdateRecruitmentAPIView.as_view()),
    path('<uuid:recruitment_id>/status', ChangeRecruitmentStatusAPIView.as_view()),
    path('<uuid:recruitment_id>/apply', ApplyRecruitmentAPIView.as_view()),
    path('<uuid:recruitment_id>/applications', ListRecruitmentApplicationsAPIView.as_view()),
    path('<uuid:recruitment_id>/applications/bulk-status', BulkApplicationStatusAPIView.as_view()),
    path('<uuid:recruitment_id>/applications/bulk-fee', BulkApplicationFeeAPIView.as_view()),
    path('<uuid:recruitment_id>/applications/message', MessageApplicantsAPIView.as_view()),
    # Announcements. 'recipients-count' is listed before the collection route
    # for readability only - they are distinct paths, not a prefix match.
    path('<uuid:recruitment_id>/announcements/recipients-count', AnnouncementRecipientsCountAPIView.as_view()),
    path('<uuid:recruitment_id>/announcements', RecruitmentAnnouncementsAPIView.as_view()),
    path('announcements/<uuid:announcement_id>', AnnouncementDetailAPIView.as_view()),
    # Shortlist. 'saved/list' is listed before the <uuid> routes for
    # readability only - "saved" can never match a uuid converter.
    path('saved/list', SavedRecruitmentsListAPIView.as_view()),
    path('<uuid:recruitment_id>/save', ToggleSaveRecruitmentAPIView.as_view()),
    path('applications/my', MyApplicationsAPIView.as_view()),
    path('applications/<uuid:application_id>/details', RecruitmentApplicationDetailAPIView.as_view()),
    path('applications/<uuid:application_id>/withdraw', WithdrawApplicationAPIView.as_view()),
    path('applications/<uuid:application_id>/status', ApplicationStatusAPIView.as_view()),
    path('applications/<uuid:application_id>/fee', ApplicationFeeAPIView.as_view()),
    path('applications/<uuid:application_id>/pass', TrialPassAPIView.as_view()),
    path('applications/<uuid:application_id>/feedback', TrialFeedbackAPIView.as_view()),
]
