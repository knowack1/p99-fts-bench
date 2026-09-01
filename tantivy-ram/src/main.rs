use std::env;
use std::fs::File;
use std::io::{BufRead, BufReader};

use tantivy::collector::TopDocs;
use tantivy::indexer::IndexWriterOptions;
use tantivy::query::QueryParser;
use tantivy::schema::{
    IndexRecordOption, Schema, TextFieldIndexing, TextOptions, INDEXED, STORED,
};
use tantivy::tokenizer::{Language, LowerCaser, SimpleTokenizer, StopWordFilter, TextAnalyzer};
use tantivy::TantivyDocument;

const TOKENIZER_NAME: &str = "standard";

fn build_standard_analyzer() -> TextAnalyzer {
    let stop_words = StopWordFilter::new(Language::English).expect("english stop words");
    TextAnalyzer::builder(SimpleTokenizer::default())
        .filter(LowerCaser)
        .filter(stop_words)
        .build()
}

fn body_text_options() -> TextOptions {
    let indexing = TextFieldIndexing::default()
        .set_tokenizer(TOKENIZER_NAME)
        .set_index_option(IndexRecordOption::WithFreqsAndPositions);
    TextOptions::default().set_indexing_options(indexing)
}

fn build_schema() -> Schema {
    let mut schema_builder = Schema::builder();
    schema_builder.add_u64_field("primary_id", INDEXED | STORED);
    schema_builder.add_text_field("body", body_text_options());
    schema_builder.build()
}

fn read_proc_kb(key: &str) -> u64 {
    let status = std::fs::read_to_string("/proc/self/status").unwrap_or_default();
    for line in status.lines() {
        if let Some(rest) = line.strip_prefix(key) {
            let digits: String = rest.chars().filter(|c| c.is_ascii_digit()).collect();
            return digits.parse().unwrap_or(0);
        }
    }
    0
}

fn rss_bytes() -> u64 {
    read_proc_kb("VmRSS:") * 1024
}

fn peak_rss_bytes() -> u64 {
    read_proc_kb("VmHWM:") * 1024
}

struct Checkpoint {
    docs: u64,
    text_bytes: u64,
    space_usage: u64,
    rss: u64,
}

fn print_header() {
    println!(
        "{:>10}  {:>14}  {:>14}  {:>14}  {:>10}  {:>10}",
        "docs", "text_bytes", "space_usage", "rss_bytes", "su/text", "rss/text"
    );
}

fn print_checkpoint(c: &Checkpoint, baseline_rss: u64) {
    let text = c.text_bytes.max(1) as f64;
    let rss_delta = c.rss.saturating_sub(baseline_rss);
    println!(
        "{:>10}  {:>14}  {:>14}  {:>14}  {:>10.3}  {:>10.3}",
        c.docs,
        c.text_bytes,
        c.space_usage,
        rss_delta,
        c.space_usage as f64 / text,
        rss_delta as f64 / text
    );
}

fn main() {
    let args: Vec<String> = env::args().collect();
    if args.len() < 2 {
        eprintln!(
            "usage: tantivy-ram <corpus.jsonl> [max_docs] [checkpoint_every] [writer_budget_mb]"
        );
        std::process::exit(2);
    }
    let corpus_path = &args[1];
    let max_docs: u64 = args.get(2).and_then(|s| s.parse().ok()).unwrap_or(0);
    let checkpoint_every: u64 = args.get(3).and_then(|s| s.parse().ok()).unwrap_or(50_000);
    let writer_budget_mb: usize = args.get(4).and_then(|s| s.parse().ok()).unwrap_or(256);

    let baseline_rss = rss_bytes();
    eprintln!("baseline rss: {} bytes", baseline_rss);

    let schema = build_schema();
    let index = tantivy::Index::create_in_ram(schema.clone());
    index
        .tokenizers()
        .register(TOKENIZER_NAME, build_standard_analyzer());

    let options = IndexWriterOptions::builder()
        .memory_budget_per_thread(writer_budget_mb * 1024 * 1024)
        .num_worker_threads(4)
        .build();
    let mut writer = index.writer_with_options(options).expect("writer");
    let reader = index.reader().expect("reader");

    let primary_id_field = schema.get_field("primary_id").unwrap();
    let body_field = schema.get_field("body").unwrap();

    let file = File::open(corpus_path).expect("open corpus");
    let buf = BufReader::with_capacity(1 << 20, file);

    let mut docs: u64 = 0;
    let mut text_bytes: u64 = 0;
    let started = std::time::Instant::now();

    print_header();

    for line in buf.lines() {
        let line = match line {
            Ok(l) => l,
            Err(_) => break,
        };
        let value: serde_json::Value = match serde_json::from_str(&line) {
            Ok(v) => v,
            Err(_) => continue,
        };
        let id = value.get("id").and_then(|v| v.as_u64()).unwrap_or(0);
        let text = value.get("text").and_then(|v| v.as_str()).unwrap_or("");
        if text.is_empty() {
            continue;
        }

        let mut doc = TantivyDocument::new();
        doc.add_u64(primary_id_field, id);
        doc.add_text(body_field, text);
        writer.add_document(doc).expect("add_document");

        docs += 1;
        text_bytes += text.len() as u64;

        if docs % checkpoint_every == 0 {
            writer.commit().expect("commit");
            reader.reload().expect("reload");
            let searcher = reader.searcher();
            let space_usage = searcher
                .space_usage()
                .expect("space usage")
                .total()
                .get_bytes();
            print_checkpoint(
                &Checkpoint {
                    docs,
                    text_bytes,
                    space_usage,
                    rss: rss_bytes(),
                },
                baseline_rss,
            );
        }

        if max_docs != 0 && docs >= max_docs {
            break;
        }
    }

    writer.commit().expect("final commit");
    reader.reload().expect("final reload");
    let searcher = reader.searcher();
    let space_usage = searcher
        .space_usage()
        .expect("space usage")
        .total()
        .get_bytes();

    println!("--- final (writer still alive) ---");
    print_checkpoint(
        &Checkpoint {
            docs,
            text_bytes,
            space_usage,
            rss: rss_bytes(),
        },
        baseline_rss,
    );

    let elapsed = started.elapsed();
    eprintln!(
        "indexed {} docs, {} text bytes in {:.1}s ({:.0} docs/s)",
        docs,
        text_bytes,
        elapsed.as_secs_f64(),
        docs as f64 / elapsed.as_secs_f64()
    );
    eprintln!("segments: {}", searcher.segment_readers().len());
    eprintln!("num_docs: {}", searcher.num_docs());

    let query_parser = QueryParser::for_index(&index, vec![body_field]);
    if let Ok(query) = query_parser.parse_query("science") {
        if let Ok(hits) = searcher.search(&query, &TopDocs::with_limit(10).order_by_score()) {
            eprintln!("sanity query 'science': {} hits", hits.len());
        }
    }

    drop(searcher);
    drop(writer);

    println!("--- after dropping writer ---");
    println!(
        "rss_delta_bytes={} space_usage_bytes={} text_bytes={} docs={}",
        rss_bytes().saturating_sub(baseline_rss),
        space_usage,
        text_bytes,
        docs
    );
    println!(
        "peak_rss_delta_bytes={}",
        peak_rss_bytes().saturating_sub(baseline_rss)
    );
}
