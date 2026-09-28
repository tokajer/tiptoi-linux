"""HTTPS-only URL opener shared by the catalog and .gme downloads."""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request

from tiptoi_linux.i18n import _


class HTTPSOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        if urllib.parse.urlparse(newurl).scheme != "https":
            # WHY: urlopen follows 301/302 to plain http by default, silently defeating the scheme check below
            raise urllib.error.URLError(
                _("refusing to follow redirect to non-https URL: {url}").format(url=newurl)
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


https_opener = urllib.request.build_opener(HTTPSOnlyRedirectHandler)
