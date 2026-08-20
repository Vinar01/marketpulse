FROM node:20-alpine AS builder
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci || npm install
COPY . .
# Vite inlines VITE_* at build time, so the API origin must be known here.
ARG VITE_API_BASE=""
ARG VITE_API_KEY="demo-key-public"
ENV VITE_API_BASE=$VITE_API_BASE VITE_API_KEY=$VITE_API_KEY
RUN npm run build

FROM nginx:1.27-alpine AS runtime
COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
