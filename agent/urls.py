from django.urls import path

from agent import views

urlpatterns = [
    path("sessions/", views.sessions_list_create, name="sessions-list-create"),
    path("sessions/<uuid:session_id>/", views.session_detail, name="session-detail"),
    path("repos/", views.repos_list, name="repos-list"),
]
