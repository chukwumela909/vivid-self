# Multi-stage Next.js build producing a small standalone runtime image.
FROM node:24-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM node:24-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .

# NEXT_PUBLIC_* vars are inlined into the client bundle at BUILD time, so the
# bot URL must be passed as a build arg (set it in Coolify's Build Variables),
# not just as a runtime env var.
ARG NEXT_PUBLIC_BOT_OFFER_URL
ENV NEXT_PUBLIC_BOT_OFFER_URL=$NEXT_PUBLIC_BOT_OFFER_URL
RUN npm run build

FROM node:24-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production \
    PORT=3000 \
    HOSTNAME=0.0.0.0

# standalone output bundles a minimal server + only the deps it needs.
COPY --from=builder /app/public ./public
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static

EXPOSE 3000
CMD ["node", "server.js"]
