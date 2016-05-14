from functools import reduce
import logging

from rest_framework.pagination import PageNumberPagination

from whiskies.command_functions import heroku_search_whiskies, \
    local_whiskey_search
from django.shortcuts import render
from django.contrib.auth.models import User
from django.views.generic import ListView
from rest_framework import generics, status
from rest_framework.permissions import IsAuthenticatedOrReadOnly
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import permissions
import operator
from django.db.models import Q, Sum, Count, Min, Max, Avg

from whiskies.models import Whiskey, Review, TagSearch, Tag, Profile, \
    TagTracker
from whiskies.serializers import UserSerializer, WhiskeySerializer,\
    ReviewSerializer, TagSearchSerializer, TagSerializer, TagTrackerSerializer, \
    AddLikedSerializer, ProfileSerializer

import cloudinary
import cloudinary.uploader
import cloudinary.api

"""
Only create/delete Whiskey in the admin.

notes: double check permissions, might need need to switch some to
OwnerOrReadOnly.
"""

logger = logging.getLogger("whiskies")
tag_logger = logging.getLogger("whiskey_tag")


def add_tag_to_whiskey(whiskey, tag):
    """
    This will increment the tag tracker for the given whiskey and tag.
    A new tracker will be created if this is the first time the tag is
    applied to the whiskey.
    """
    tracker = TagTracker.objects.filter(whiskey=whiskey, tag=tag).first()
    if not tracker:
        tracker = TagTracker.objects.create(whiskey=whiskey, tag=tag)
    tracker.add_count()
    tracker.save()



class ShootPagination(PageNumberPagination):
    page_size = 12
    page_size_query_param = 'page_size'
    max_page_size = 120


class UserListCreate(generics.ListCreateAPIView):
    queryset = User.objects.all()
    serializer_class = UserSerializer
    permission_classes = [
        permissions.AllowAny
    ]


class UserDetail(generics.RetrieveUpdateAPIView):
    queryset = User.objects.all()
    serializer_class = UserSerializer


class WhiskeyList(generics.ListAPIView):
    queryset = Whiskey.objects.all()
    serializer_class = WhiskeySerializer


class WhiskeyDetail(generics.RetrieveAPIView):
    queryset = Whiskey.objects.all()
    serializer_class = WhiskeySerializer


class ReviewListCreate(generics.ListCreateAPIView):
    """
    To create a review send a POST request with title, text, whiskey id, and
     an optional rating from 1-100.
     Example: {"title": "Test Title", "text": "Review body text",
     "whiskey": 5, "rating": 90}
    """
    queryset = Review.objects.all()
    serializer_class = ReviewSerializer
    permission_classes = (IsAuthenticatedOrReadOnly,)

    def perform_create(self, serializer):
        whiskey_id = self.request.data["whiskey"]

        serializer.save(user=self.request.user,
                        whiskey=Whiskey.objects.get(pk=whiskey_id))


class ReviewDetailUpdateDelete(generics.RetrieveUpdateDestroyAPIView):
    queryset = Review.objects.all()
    serializer_class = ReviewSerializer
    permission_classes = (IsAuthenticatedOrReadOnly,)


class TagSearchListCreate(generics.ListCreateAPIView):
    queryset = TagSearch.objects.all()
    serializer_class = TagSearchSerializer

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


class UserTagSearchList(generics.ListAPIView):

    serializer_class = TagSearchSerializer

    def get_queryset(self):
        return TagSearch.objects.filter(
            user=self.request.user).order_by("created_at")


class TagSearchDetailUpdateDelete(generics.RetrieveUpdateDestroyAPIView):
    queryset = TagSearch.objects.all()
    serializer_class = TagSearchSerializer
    permission_classes = (IsAuthenticatedOrReadOnly,)


class TagListCreate(generics.ListCreateAPIView):
    queryset = Tag.objects.all()
    serializer_class = TagSerializer
    permission_classes = (IsAuthenticatedOrReadOnly,)


class TagDetailUpdateDelete(generics.RetrieveUpdateDestroyAPIView):
    queryset = Tag.objects.all()
    serializer_class = TagSerializer
    permission_classes = (IsAuthenticatedOrReadOnly,)


class WhiskeyLikeUpdate(APIView):
    """
    Put request needs a whiskey_id, action ['add', 'remove'], and
    opinion ['like', 'dislike'].
    Example: {"whiskey_id": 5, "action": "remove", "opinion": "like"}
    """

    def put(self, request, format=None):

        user = request.user
        serializer = AddLikedSerializer(user, data=request.data)
        if serializer.is_valid():
            serializer.save(whiskey_id=request.data["whiskey_id"],
                            action=request.data["action"],
                            opinion=request.data["opinion"])

            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class LikedWhiskeyList(generics.ListAPIView):
    """
    A GET request returns all of the requesting user's liked whiskies.
    """
    queryset = Whiskey.objects.all()
    serializer_class = WhiskeySerializer

    def get_queryset(self):

        return self.request.user.profile.liked_whiskies.all()


class DislikedWhiskeyList(generics.ListAPIView):
    """
    A GET request returns all of the requesting user's disliked whiskies.
    """
    queryset = Whiskey.objects.all()
    serializer_class = WhiskeySerializer

    def get_queryset(self):

        return self.request.user.profile.disliked_whiskies.all()


class SearchList(generics.ListCreateAPIView):
    """
    Filter whiskies based on three optional parameters:
    tags: The titles of any Tags in the database, the endpoint /tag provides
    a list.
    price: 1, 2, or 3 for low, mid, and/or high priced whiskies.
    region: Filter by one or more regions.

    An example of a valid query: /shoot/?tags=chocolate&region=highland&price=1

    The price ranges are broken down as:
    1: price <=40
    2: 40< price <= 75
    3: 75< price
    """

    serializer_class = WhiskeySerializer
    pagination_class = ShootPagination

    def get_queryset(self):

        if self.request.user.pk and self.request.user.profile.disliked_whiskies.all():

            dislikes = self.request.user.profile.disliked_whiskies.all().\
                values_list('pk', flat=True)

            qs = Whiskey.objects.exclude(pk__in=dislikes)
        else:
            qs = Whiskey.objects.all()

        if "region" in self.request.query_params:
            regions = self.request.query_params['region'].split(',')
            regions = [x.capitalize() for x in regions]
            qs = qs.filter(region__in=regions)

        if "price" in self.request.query_params:
            price_ranges = {'1': [x for x in range(1,41)],
                            '2': [x for x in range(41, 76)],
                            '3': [x for x in range(76, 300)]}
            prices = []
            for price in self.request.query_params["price"]:
                prices += price_ranges[price]

            qs = qs.filter(price__in=prices)

        if "tags" not in self.request.query_params:
            return qs
        else:
            tag_titles = self.request.query_params['tags'].split(',')
            a = qs.filter(tagtracker__tag__title__in=tag_titles)
            b = a.annotate(tag_count=Sum('tagtracker__count'))
            results = b.order_by('-tag_count')

            return results


class TextSearchBox(APIView):
    """
    Elasticsearch of Whiskey titles.
    """

    def get(self, request, format=None):
        terms = request.query_params['terms']
        res = heroku_search_whiskies(terms.split(","))
        hits = res['hits']['hits']
        return Response([hit["_source"] for hit in hits])


"""
Unused views for local testing or future development.
"""

class AllWhiskey(ListView):
    template_name = "whiskies/all_whiskies.html"
    queryset = Whiskey.objects.all()
    context_object_name = "whiskies"


#  No longer used, just here in case I need to swap elasticsearch out.
class PlaceholderSearch(generics.ListAPIView):
    """
    Returns a queryset of all whiskies with a title that contains 1 or more
    of the search terms.

    example: /searchbox/?terms=term1,term2
    """

    serializer_class = WhiskeySerializer

    def get_queryset(self):

        # if "terms" not in self.request.query_params:
        #     return []

        terms = self.request.query_params['terms'].split(',')

        query = reduce(operator.or_, (
            Q(title__icontains=item) for item in terms)
                       )

        qs = Whiskey.objects.filter(query)
        return qs


class TestSearch(APIView):
    """
    For converting the shoot/ endpoint from sql queries to elasticsearch.
    Not currently in use.
    """

    def get(self, request, format=None):
        terms = request.query_params['terms']
        #res = heroku_search_whiskies(terms.split(","))
        res = local_whiskey_search(terms.split(","))
        ids = [item["_source"]["id"] for item in res['hits']['hits']]
        qs = Whiskey.objects.filter(id__in=ids)
        sorted_qs = sorted(qs, key=lambda x: x.tag_match(terms.split(",")), reverse=True)
        serializer = WhiskeySerializer(sorted_qs, many=True)

        return Response(serializer.data)
