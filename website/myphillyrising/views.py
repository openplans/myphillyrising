import base64
import hashlib
import hmac
import json
import time
from django.conf import settings
from django.contrib.auth import logout
from django.core.urlresolvers import reverse
from django.utils.timezone import now, timedelta
from django.db.models import Sum, Q
from django.views.generic import TemplateView, FormView
from rest_framework.permissions import IsAuthenticatedOrReadOnly
from rest_framework.routers import DefaultRouter
from rest_framework.viewsets import ModelViewSet
from myphillyrising.forms import ChooseNeighborhoodForm
from myphillyrising.models import Neighborhood, User, UserProfile, UserAction, Neighbor
from myphillyrising.serializers import NeighborhoodSerializer, UserSerializer, LoggedInUserSerializer, ActionSerializer
from myphillyrising.services import default_twitter_service
from proxy.views import proxy_view
from utils.views import EnsureCsrfCookieMixin


class MyPhillyRisingViewMixin (object):
    def get_user_queryset(self):
        qs =  (
            Neighbor.objects.all()

            # Only select users that have profiles.
            .select_related('profile')
            .exclude(profile__isnull=True)

            # Pre-select the neighborhood object.
            .select_related('profile__neighborhood')

            # Prefetch the action objects so that score calculations are fast.
            .prefetch_related('actions')
        )

        return qs

    def get_neighborhood_queryset(self):
        return (
            Neighborhood.objects.all()\

            # For points, only use those that are in the last 30 days. Also
            # include actions that are null, because those will correspond to
            # users that yet have no points.
            .prefetch_related('profiles__user__actions')

            .order_by('tag')
        )

    def get_disqus_sso_message(self, user):
        if user.is_authenticated():
            try:
                data = {
                    'id': str(user.id) + settings.DISQUS_ACCOUNT_UNIQUIFIER,
                    'username': user.username,
                    'email': user.email,
                    'avatar': user.profile.avatar_url
                }
            except UserProfile.DoesNotExist:
                logout(self.request)
                data = {}
        else:
            data = {}

        message = base64.b64encode(json.dumps(data))
        return message

    def get_disqus_sso_timestamp(self):
        timestamp = int(time.time())
        return timestamp

    def get_disqus_sso_signature(self, message, timestamp):
        signature = hmac.HMAC(
            str(settings.DISQUS_SECRET_KEY),
            '%s %s' % (message, timestamp),
            hashlib.sha1).hexdigest()
        return signature

    def get_disqus_sso_auth_string(self, user):
        message = self.get_disqus_sso_message(user)
        timestamp = self.get_disqus_sso_timestamp()
        signature = self.get_disqus_sso_signature(message, timestamp)
        return ' '.join([message, signature, str(timestamp)])


class AppView (MyPhillyRisingViewMixin, EnsureCsrfCookieMixin, TemplateView):
    template_name = 'myphillyrising/index.html'

    def get_neighborhood_data(self):
        neighborhoods = self.get_neighborhood_queryset()
        serializer = NeighborhoodSerializer(neighborhoods)
        return serializer.data

    def get_current_user_data(self):
        current_user = self.request.user
        if current_user.is_authenticated():
            try:
                current_user.profile
            except UserProfile.DoesNotExist:
                return {}
            else:
                queryset = self.get_user_queryset()
                serializer = LoggedInUserSerializer(queryset.get(pk=current_user.pk))
                return serializer.data
        else:
            return {}

    def get_context_data(self, **kwargs):
        context = super(AppView, self).get_context_data(**kwargs)
        context['neighborhood_data'] = self.get_neighborhood_data()
        context['current_user_data'] = self.get_current_user_data()
        context['twitter_config'] = json.dumps(default_twitter_service.get_config())
        context['NS'] = 'MyPhillyRising'
        context['disqus_sso_auth'] = self.get_disqus_sso_auth_string(self.request.user)
        return context


class ChooseNeighborhoodView (MyPhillyRisingViewMixin, FormView):
    template_name = 'myphillyrising/choose-neighborhood.html'
    form_class = ChooseNeighborhoodForm

    def get_context_data(self, **kwargs):
        context = super(ChooseNeighborhoodView, self).get_context_data(**kwargs)
        context['neighborhoods'] = Neighborhood.objects.all()
        context['auth_provider'] = (
            self.request.GET.get('auth_provider') or
            self.request.session.get('auth_provider'))
        return context

    def form_valid(self, form):
        self.request.session['neighborhood'] = form.cleaned_data['neighborhood']
        self.auth_provider = form.cleaned_data['auth_provider']
        return super(ChooseNeighborhoodView, self).form_valid(form)

    def get_success_url(self):
        return reverse('socialauth_complete', args=(self.auth_provider,))


class SiteMapView (MyPhillyRisingViewMixin, TemplateView):
    template_name = 'myphillyrising/sitemap.xml'

    def get_neighborhood_data(self):
        neighborhoods = self.get_neighborhood_queryset()
        serializer = NeighborhoodSerializer(neighborhoods)
        return serializer.data

    def get_context_data(self, **kwargs):
        context = super(SiteMapView, self).get_context_data(**kwargs)
        context['neighborhood_data'] = self.get_neighborhood_data()
        return context


class UserViewSet (MyPhillyRisingViewMixin, ModelViewSet):
    model = User
    serializer_class = UserSerializer
    paginate_by = 20
    permission_classes = (IsAuthenticatedOrReadOnly,)

    def get_queryset(self):
        queryset = self.get_user_queryset()
        neighborhoods = self.request.GET.getlist('neighborhood')
        if (neighborhoods):
            queryset = queryset.filter(profile__neighborhood__tag_id__in=neighborhoods)

        return queryset

    def get_serializer_class(self, *args, **kwargs):
        if hasattr(self, 'object') and self.object is not None:
            if self.object.pk == self.request.user.pk:
                return LoggedInUserSerializer
        return super(UserViewSet, self).get_serializer_class(*args, **kwargs)


class ActionViewSet (MyPhillyRisingViewMixin, ModelViewSet):
    model = UserAction
    serializer_class = ActionSerializer
    paginate_by = 20
    permission_classes = (IsAuthenticatedOrReadOnly,)


def phila_gis_proxy_view(request, path):
    """
    A small proxy for Philly GIS services, so that they are accessible over
    HTTPS.
    """
    root = 'http://gis.phila.gov/ArcGIS/rest/services/PhilaGov/'
    return proxy_view(request, root + path)


# Views
app_view = AppView.as_view()
choose_neighborhood = ChooseNeighborhoodView.as_view()
robots_view = TemplateView.as_view(template_name='myphillyrising/robots.txt')
sitemap_view = SiteMapView.as_view()

# Setup the API routes
api_router = DefaultRouter(trailing_slash=False)
api_router.register('users', UserViewSet)
api_router.register('actions', ActionViewSet)
