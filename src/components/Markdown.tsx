import type { ReactNode } from 'react';

// The small Markdown subset app.py uses: paragraphs, "### " headings and **bold**.
function inline(text: string): ReactNode[] {
  return text.split(/(\*\*[^*]+\*\*)/g).map((part, i) =>
    part.startsWith('**') && part.endsWith('**') && part.length > 4 ? (
      <strong key={i}>{part.slice(2, -2)}</strong>
    ) : (
      part
    ),
  );
}

export function Markdown({ body }: { body: string }) {
  return (
    <>
      {body.split(/\n{2,}/).map((block, i) =>
        block.startsWith('### ') ? (
          <h3 key={i} className="md-h3">
            {inline(block.slice(4))}
          </h3>
        ) : (
          <p key={i} className="md-p">
            {inline(block)}
          </p>
        ),
      )}
    </>
  );
}
