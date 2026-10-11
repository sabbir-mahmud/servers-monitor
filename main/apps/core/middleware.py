class HtmxMiddleware:
    """Set `request.htmx` so views can return a fragment instead of a full page."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.htmx = request.headers.get("HX-Request") == "true"
        return self.get_response(request)
