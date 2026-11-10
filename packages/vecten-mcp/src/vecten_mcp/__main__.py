import uvicorn

from vecten_mcp.app import app

# Only NGINX on the Docker network reaches this port; "*" would trust the client-written entry.
FORWARDED_ALLOW_IPS = "127.0.0.0/8,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"


def main():
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8001,
        proxy_headers=True,
        forwarded_allow_ips=FORWARDED_ALLOW_IPS,
    )


if __name__ == "__main__":
    main()
