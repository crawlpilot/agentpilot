import { useState } from 'react'
import { Copy, Eye, EyeOff, Loader2 } from 'lucide-react'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from '@/components/ui/dialog'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Button } from '@/components/ui/button'
import { useToast } from '@/components/ui/toast'
import { useCdpInfo } from '@/hooks/useCdpInfo'

// Masks only the `api_key=` value embedded in the discovery doc's
// `webSocketDebuggerUrl` -- the scheme/host/path are useful to see at a
// glance (and aren't secret), only the credential in the query string is.
function maskWsUrl(url: string): string {
  return url.replace(/([?&]api_key=)([^&]+)/, (_match, prefix: string, key: string) => {
    if (key.length <= 12) return `${prefix}••••••••`
    return `${prefix}${key.slice(0, 8)}${'•'.repeat(key.length - 12)}${key.slice(-4)}`
  })
}

function buildPlaywrightPython(wsUrl: string): string {
  return [
    'import asyncio',
    'from playwright.async_api import async_playwright',
    '',
    'async def main():',
    '    async with async_playwright() as p:',
    `        browser = await p.chromium.connect_over_cdp("${wsUrl}")`,
    '        context = browser.contexts[0]',
    '        page = await context.new_page()',
    '        await page.goto("https://example.com")',
    '        print(await page.title())',
    '        await browser.close()  # disconnects -- the remote session stays up',
    '',
    'asyncio.run(main())',
  ].join('\n')
}

function buildPlaywrightNode(wsUrl: string): string {
  return [
    "const { chromium } = require('playwright');",
    '',
    '(async () => {',
    `  const browser = await chromium.connectOverCDP('${wsUrl}');`,
    '  const context = browser.contexts()[0];',
    '  const page = await context.newPage();',
    "  await page.goto('https://example.com');",
    '  console.log(await page.title());',
    '  await browser.close(); // disconnects -- the remote session stays up',
    '})();',
  ].join('\n')
}

function buildPuppeteerNode(wsUrl: string): string {
  return [
    "const puppeteer = require('puppeteer-core');",
    '',
    '(async () => {',
    `  const browser = await puppeteer.connect({ browserWSEndpoint: '${wsUrl}' });`,
    '  const page = (await browser.pages())[0] ?? (await browser.newPage());',
    "  await page.goto('https://example.com');",
    '  console.log(await page.title());',
    '  await browser.disconnect(); // the remote session stays up',
    '})();',
  ].join('\n')
}

function CopyButton({ text, label }: { text: string; label: string }) {
  const { toast } = useToast()
  return (
    <Button
      size="sm"
      variant="ghost"
      className="gap-1 text-xs"
      onClick={async () => {
        await navigator.clipboard.writeText(text)
        toast({ title: `${label} copied` })
      }}
    >
      <Copy className="size-3.5" />
      Copy
    </Button>
  )
}

export function CdpConnectDialog({
  sessionId,
  open,
  onOpenChange,
}: {
  sessionId: string
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const [revealed, setRevealed] = useState(false)
  const { data, isPending, isError, error } = useCdpInfo(sessionId, open)

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Connect via CDP</DialogTitle>
          <DialogDescription>
            Standard Chrome DevTools Protocol -- drive this browser directly from Playwright, Puppeteer,
            or any other CDP client, same as you would a local instance.
          </DialogDescription>
        </DialogHeader>

        {isPending && (
          <div className="flex items-center gap-2 py-8 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" />
            Fetching endpoint...
          </div>
        )}

        {isError && (
          <p className="py-4 text-sm text-destructive">
            Couldn't fetch the CDP endpoint: {error instanceof Error ? error.message : 'unknown error'}
          </p>
        )}

        {data && (
          <div className="flex flex-col gap-4">
            <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs text-muted-foreground">
              <span>Browser</span>
              <span className="text-foreground">{data.Browser}</span>
              <span>Protocol version</span>
              <span className="text-foreground">{data['Protocol-Version']}</span>
            </div>

            <div className="flex flex-col gap-1.5">
              <div className="flex items-center justify-between">
                <span className="text-xs font-medium text-muted-foreground">WebSocket endpoint</span>
                <div className="flex items-center gap-1">
                  <Button size="sm" variant="ghost" className="gap-1 text-xs" onClick={() => setRevealed((v) => !v)}>
                    {revealed ? <EyeOff className="size-3.5" /> : <Eye className="size-3.5" />}
                    {revealed ? 'Hide' : 'Reveal'}
                  </Button>
                  <CopyButton text={data.webSocketDebuggerUrl} label="Endpoint URL" />
                </div>
              </div>
              <pre className="overflow-x-auto rounded-md bg-muted p-2 text-xs">
                {revealed ? data.webSocketDebuggerUrl : maskWsUrl(data.webSocketDebuggerUrl)}
              </pre>
            </div>

            <Tabs defaultValue="playwright-py">
              <TabsList>
                <TabsTrigger value="playwright-py">Playwright (Python)</TabsTrigger>
                <TabsTrigger value="playwright-js">Playwright (Node)</TabsTrigger>
                <TabsTrigger value="puppeteer-js">Puppeteer (Node)</TabsTrigger>
              </TabsList>
              {(
                [
                  ['playwright-py', buildPlaywrightPython(data.webSocketDebuggerUrl)],
                  ['playwright-js', buildPlaywrightNode(data.webSocketDebuggerUrl)],
                  ['puppeteer-js', buildPuppeteerNode(data.webSocketDebuggerUrl)],
                ] as const
              ).map(([value, code]) => (
                <TabsContent key={value} value={value} className="flex flex-col gap-1.5">
                  <div className="flex justify-end">
                    <CopyButton text={code} label="Snippet" />
                  </div>
                  <pre className="max-h-64 overflow-auto rounded-md bg-muted p-3 text-xs">{code}</pre>
                </TabsContent>
              ))}
            </Tabs>
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}
