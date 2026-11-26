"use client";

import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import styles from "./AssistantMessage.module.css";

const allowedElements = [
  "p", "br", "strong", "em", "del", "h1", "h2", "h3", "h4", "h5", "h6",
  "ul", "ol", "li", "blockquote", "hr", "pre", "code",
  "table", "thead", "tbody", "tr", "th", "td", "a",
];

export default function AssistantMessage({ text }: { text: string }) {
  return <div className={styles.message}>
    <Markdown
      remarkPlugins={[remarkGfm]}
      skipHtml
      allowedElements={allowedElements}
      components={{
        // Product navigation stays in the panel's verified control links.
        a: ({ children }) => <span>{children}</span>,
        table: ({ children }) => <div className={styles.tableScroll} role="region" aria-label="Response table" tabIndex={0}>
          <table>{children}</table>
        </div>,
      }}
    >{text}</Markdown>
  </div>;
}
