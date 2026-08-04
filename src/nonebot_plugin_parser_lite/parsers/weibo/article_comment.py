import re

from bs4 import BeautifulSoup
from msgspec import Struct
from msgspec.json import Decoder

from ...utils.format import replace_placeholder_to_sticker

WEIBO_PATTERN = re.compile(r"\[(?P<name>[^]]+)\]")


class UserInfo(Struct):
    id: int
    screen_name: str
    description: str
    profile_image_url: str


class ArticleComment(Struct):
    created_at_unix: int
    text: str
    user_info: UserInfo

    @property
    def content(self):
        # 微博 API text 是 HTML (<a>/<br>/<span class=url-icon>) - 正确解析为纯文本
        plain = BeautifulSoup(self.text, "html.parser").get_text(
            separator="\n", strip=True)
        return replace_placeholder_to_sticker(plain, WEIBO_PATTERN, "weibo")


class ArticleCommentData(Struct):
    total_number: int
    comments: list[ArticleComment]


class ArticleCommentWrapper(Struct):
    data: ArticleCommentData


decoder = Decoder(ArticleCommentWrapper)
