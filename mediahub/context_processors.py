from django.conf import settings


def app_shell(_request):
    return {"APP_NAME": settings.APP_NAME}
