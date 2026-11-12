"""測試以實際編輯頁取得基底版本，與瀏覽器表單一致。"""
import re


def post_submission(client, *, data, **kwargs):
    data = dict(data)
    if "base_version" not in data:
        response = client.get("/form", query_string={"term": data.get("term", "")})
        match = re.search(r'name="base_version" value="([^"]*)"', response.get_data(as_text=True))
        data["base_version"] = match.group(1) if match else "0"
    return client.post("/submit", data=data, **kwargs)


def post_tracking(client, url, *, data, **kwargs):
    data = dict(data)
    if "expected_version" not in data:
        page = client.get("/office/documents").get_data(as_text=True)
        import html
        for form in re.findall(r"<form\b.*?</form>", page, re.S):
            token = re.search(r'name="token" value="([^"]*)"', form)
            version = re.search(r'name="expected_version" value="([^"]*)"', form)
            if ('action="' + url + '"') in form and token and html.unescape(token.group(1)) == data.get("token") and version:
                data["expected_version"] = html.unescape(version.group(1))
                break
    return client.post(url, data=data, **kwargs)


def post_committee_reply(client, *, data, **kwargs):
    data = dict(data)
    page = client.get("/office/committees").get_data(as_text=True)
    import html
    for form in re.findall(r"<form\b.*?</form>", page, re.S):
        email = re.search(r'name="email" value="([^"]*)"', form)
        binding = re.search(r'name="identity_binding" value="([^"]*)"', form)
        if email and binding and html.unescape(email.group(1)) == data.get("email"):
            data.setdefault("identity_binding", binding.group(1))
    return client.post("/office/committees/reply", data=data, **kwargs)
