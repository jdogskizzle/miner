FROM python:3.12-alpine
WORKDIR /app
COPY control.py index.html /app/
# Shared sign-in library (the formlogin build context, see docker-compose.yml).
COPY --from=formlogin formlogin /app/formlogin
CMD ["python", "-u", "/app/control.py"]
