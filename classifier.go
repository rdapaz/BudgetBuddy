// classifier: concurrent Claude-based transaction classifier for BudgetBuddy.
//
// Two modes:
//
//	-mode budget (default)  Classifies into the 19 household budget categories used
//	                        by the PySide6 GUI (see gui/categories.py). Reads rows where
//	                        budget_category IS NULL and writes budget_category.
//	-mode tax               Legacy tax-deduction categories. Reads rows where
//	                        ai_classified = 0 and writes the category column.
//
// The API key is read from the config file (-c, default config.json) or, if that
// file is absent, from the ANTHROPIC_API_KEY environment variable.
package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/anthropics/anthropic-sdk-go"
	"github.com/anthropics/anthropic-sdk-go/option"
	_ "github.com/mattn/go-sqlite3"
)

// Current Sonnet. Thinking is on by default on this model, so the reply is read
// by block type (not position) and max_tokens leaves room for it; effort "low"
// keeps thinking minimal for a one-word classification.
const model = "claude-sonnet-5-5"

type Config struct {
	AnthropicAPIKey string `json:"anthropic_api_key"`
}

type Transaction struct {
	ID          int
	Date        string
	Description string
	Amount      float64
}

type ClassificationResult struct {
	ID       int
	Category string
	Error    error
}

// Category is one entry in a classification scheme.
type Category struct {
	ID          string
	Description string
}

// Mode bundles everything that differs between the budget and tax schemes.
type Mode struct {
	Name       string
	Column     string     // DB column that receives the result
	Categories []Category // valid outputs, in prompt order
	Fallback   string     // used when the model returns something unexpected
	Prompt     func(categoryList, desc string, amount float64) string
	SelectSQL  string
	UpdateSQL  string
}

// budgetCategories mirrors gui/categories.py (minus "uncategorised", which is
// never a classification target). Keep the two in sync.
var budgetCategories = []Category{
	{"housing", "Rent, mortgage, rates, strata, home repairs, Bunnings"},
	{"utilities", "Electricity, gas, water, internet, mobile phone"},
	{"fuel", "Petrol, diesel, EV charging"},
	{"car", "Rego, service, tyres, tolls, parking, car wash"},
	{"public_transport", "Train, bus, ferry, Opal, Myki, rideshare, taxis"},
	{"groceries", "Supermarkets, butcher, bakery, fruit & veg"},
	{"household", "Cleaning, homewares, Kmart, Target, Big W"},
	{"dining_out", "Restaurants, cafes, takeaway, UberEats, Menulog"},
	{"health", "GP, dentist, optometrist, pharmacy, hospital, Medicare gap"},
	{"fitness", "Gym, sports, equipment, activewear"},
	{"children", "Childcare, school fees, uniforms, kids activities, tutoring"},
	{"entertainment", "Streaming, movies, gaming, concerts, hobbies, subscriptions, apps"},
	{"clothing", "Clothes, shoes, accessories"},
	{"personal_care", "Hair, beauty, grooming, skincare"},
	{"insurance", "Home, car, health, life, pet insurance"},
	{"fees_charges", "Bank fees, interest charges, ATM fees, donations, gifts"},
	{"work_deductible", "Software, training, books, professional memberships — tax deductible"},
	{"other", "Anything that doesn't fit the above"},
}

var taxCategories = []Category{
	{"software", "Software purchases, SaaS subscriptions, development tools"},
	{"professional membership", "Professional organization memberships, certifications"},
	{"technical library", "Books, technical publications, O'Reilly, Manning, etc."},
	{"magazines and journals", "Technical magazines, IEEE, ACM subscriptions"},
	{"training", "Online courses, conferences, workshops, training materials"},
	{"not work related", "Personal purchases, food, entertainment, general shopping"},
	{"other", "Work-related but doesn't fit other categories"},
}

var budgetMode = Mode{
	Name:       "budget",
	Column:     "budget_category",
	Categories: budgetCategories,
	Fallback:   "other",
	// Same wording as gui/workers/classify_worker.py so both engines behave alike.
	Prompt: func(categoryList, desc string, amount float64) string {
		return fmt.Sprintf(`Classify this Australian credit card transaction into exactly one category.

Categories:
%s

Transaction: %s
Amount: $%.2f

Respond with ONLY the category id (e.g. "groceries"), nothing else.`, categoryList, desc, amount)
	},
	SelectSQL: `
		SELECT id, trans_date, description, amount
		FROM transactions
		WHERE budget_category IS NULL
		ORDER BY trans_date DESC`,
	UpdateSQL: `UPDATE transactions SET budget_category = ?, ai_classified = 1 WHERE id = ?`,
}

var taxMode = Mode{
	Name:       "tax",
	Column:     "category",
	Categories: taxCategories,
	Fallback:   "other",
	Prompt: func(categoryList, desc string, amount float64) string {
		return fmt.Sprintf(`Given this credit card transaction, classify it into one of these categories for tax deduction purposes:

Categories:
%s

Transaction:
Description: %s
Amount: $%.2f

Context: This is for an OT cybersecurity professional in industrial control systems.

Respond with ONLY the category name in lowercase, nothing else.`, categoryList, desc, amount)
	},
	SelectSQL: `
		SELECT id, trans_date, description, amount
		FROM transactions
		WHERE ai_classified = 0 OR ai_classified IS NULL
		ORDER BY trans_date DESC`,
	UpdateSQL: `UPDATE transactions SET category = ?, ai_classified = 1 WHERE id = ?`,
}

func (m Mode) categoryList() string {
	var b strings.Builder
	for _, c := range m.Categories {
		fmt.Fprintf(&b, "- %q: %s\n", c.ID, c.Description)
	}
	return strings.TrimRight(b.String(), "\n")
}

func (m Mode) isValid(id string) bool {
	for _, c := range m.Categories {
		if c.ID == id {
			return true
		}
	}
	return false
}

// loadAPIKey reads the key from the config file, falling back to the
// ANTHROPIC_API_KEY environment variable when the file does not exist.
func loadAPIKey(configPath string) (string, error) {
	data, err := os.ReadFile(configPath)
	switch {
	case err == nil:
		var config Config
		if err := json.Unmarshal(data, &config); err != nil {
			return "", fmt.Errorf("failed to parse config %s: %w", configPath, err)
		}
		if config.AnthropicAPIKey == "" {
			return "", fmt.Errorf("anthropic_api_key not found in %s", configPath)
		}
		return config.AnthropicAPIKey, nil
	case errors.Is(err, os.ErrNotExist):
		if key := os.Getenv("ANTHROPIC_API_KEY"); key != "" {
			return key, nil
		}
		return "", fmt.Errorf("config file %s not found and ANTHROPIC_API_KEY is not set", configPath)
	default:
		return "", fmt.Errorf("failed to read config: %w", err)
	}
}

// ensureColumn adds the mode's target column if the database predates it
// (the GUI does the same migration in gui/models/database.py).
func ensureColumn(db *sql.DB, column string) error {
	rows, err := db.Query(`PRAGMA table_info(transactions)`)
	if err != nil {
		return err
	}
	defer rows.Close()

	for rows.Next() {
		var (
			cid        int
			name, typ  string
			notnull    int
			dfltValue  sql.NullString
			primaryKey int
		)
		if err := rows.Scan(&cid, &name, &typ, &notnull, &dfltValue, &primaryKey); err != nil {
			return err
		}
		if name == column {
			return nil
		}
	}
	if err := rows.Err(); err != nil {
		return err
	}

	fmt.Printf("Adding missing column %s to transactions table\n", column)
	_, err = db.Exec(fmt.Sprintf(`ALTER TABLE transactions ADD COLUMN %s TEXT`, column))
	return err
}

func getUnclassifiedTransactions(db *sql.DB, m Mode) ([]Transaction, error) {
	rows, err := db.Query(m.SelectSQL)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var transactions []Transaction
	for rows.Next() {
		var t Transaction
		if err := rows.Scan(&t.ID, &t.Date, &t.Description, &t.Amount); err != nil {
			return nil, err
		}
		transactions = append(transactions, t)
	}
	return transactions, rows.Err()
}

func classifyTransaction(ctx context.Context, client *anthropic.Client, m Mode, categoryList string, t Transaction) (string, error) {
	prompt := m.Prompt(categoryList, t.Description, t.Amount)

	message, err := client.Messages.New(ctx, anthropic.MessageNewParams{
		Model:     anthropic.F(model),
		MaxTokens: anthropic.F(int64(256)),
		Messages: anthropic.F([]anthropic.MessageParam{
			anthropic.NewUserMessage(anthropic.NewTextBlock(prompt)),
		}),
	}, option.WithJSONSet("output_config", map[string]string{"effort": "low"}))
	if err != nil {
		return "", err
	}

	var text string
	for _, block := range message.Content {
		if block.Type == anthropic.ContentBlockTypeText {
			text = block.Text
			break
		}
	}
	if text == "" {
		return "", fmt.Errorf("no text block in response (stop_reason=%s)", message.StopReason)
	}

	category := strings.ToLower(strings.TrimSpace(strings.Trim(text, `"`)))
	if m.isValid(category) {
		return category, nil
	}
	fmt.Printf("  ! unexpected category %q for %q -> %s\n", category, truncate(t.Description, 40), m.Fallback)
	return m.Fallback, nil
}

func classifyWorker(ctx context.Context, client *anthropic.Client, m Mode, categoryList string, jobs <-chan Transaction, results chan<- ClassificationResult, wg *sync.WaitGroup) {
	defer wg.Done()
	for t := range jobs {
		select {
		case <-ctx.Done():
			return
		default:
			category, err := classifyTransaction(ctx, client, m, categoryList, t)
			results <- ClassificationResult{ID: t.ID, Category: category, Error: err}
		}
	}
}

func updateTransactionCategory(db *sql.DB, m Mode, id int, category string) error {
	_, err := db.Exec(m.UpdateSQL, category, id)
	return err
}

func main() {
	dbPath := flag.String("d", "", "Path to SQLite database file (required)")
	configPath := flag.String("c", "config.json", "Path to config file (falls back to $ANTHROPIC_API_KEY if missing)")
	workers := flag.Int("w", 10, "Number of concurrent workers")
	modeName := flag.String("mode", "budget", `Classification scheme: "budget" (19 household categories, GUI-compatible) or "tax" (legacy tax-deduction categories)`)
	dryRun := flag.Bool("dry-run", false, "Show what would be classified without updating database")
	listOnly := flag.Bool("list", false, "Print the category list for the selected mode and exit")
	flag.Parse()

	var m Mode
	switch strings.ToLower(*modeName) {
	case "budget":
		m = budgetMode
	case "tax":
		m = taxMode
	default:
		log.Fatalf("Error: unknown -mode %q (use \"budget\" or \"tax\")", *modeName)
	}

	if *listOnly {
		fmt.Println(m.categoryList())
		return
	}

	if *dbPath == "" {
		log.Fatal("Error: -d (database path) is required")
	}

	apiKey, err := loadAPIKey(*configPath)
	if err != nil {
		log.Fatalf("Error: %v", err)
	}

	db, err := sql.Open("sqlite3", *dbPath)
	if err != nil {
		log.Fatalf("Error opening database: %v", err)
	}
	defer db.Close()

	// Schema migration only (adds the target column if absent); never touches rows.
	if err := ensureColumn(db, m.Column); err != nil {
		log.Fatalf("Error preparing database: %v", err)
	}

	transactions, err := getUnclassifiedTransactions(db, m)
	if err != nil {
		log.Fatalf("Error fetching transactions: %v", err)
	}

	if len(transactions) == 0 {
		fmt.Printf("✓ No unclassified transactions found (mode: %s)\n", m.Name)
		return
	}

	totalCount := len(transactions)
	fmt.Printf("Mode: %s (%d categories, writes %s)\n", m.Name, len(m.Categories), m.Column)
	fmt.Printf("Found %d unclassified transactions\n", totalCount)
	fmt.Printf("Using %d concurrent workers\n\n", *workers)

	if *dryRun {
		fmt.Println("DRY RUN MODE - No database updates will be made")
		fmt.Println()
	}

	client := anthropic.NewClient(option.WithAPIKey(apiKey))
	categoryList := m.categoryList()

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Minute)
	defer cancel()

	jobs := make(chan Transaction, totalCount)
	results := make(chan ClassificationResult, totalCount)

	var wg sync.WaitGroup
	for i := 0; i < *workers; i++ {
		wg.Add(1)
		go classifyWorker(ctx, client, m, categoryList, jobs, results, &wg)
	}

	transMap := make(map[int]Transaction, totalCount)
	for _, t := range transactions {
		transMap[t.ID] = t
		jobs <- t
	}
	close(jobs)

	go func() {
		wg.Wait()
		close(results)
	}()

	var processed int64
	successful := 0
	failed := 0
	categoryCount := make(map[string]int)
	startTime := time.Now()

	for result := range results {
		current := atomic.AddInt64(&processed, 1)

		if result.Error != nil {
			fmt.Printf("✗ (%d/%d) ID %d failed: %v\n", current, totalCount, result.ID, result.Error)
			failed++
			continue
		}

		trans := transMap[result.ID]
		if !*dryRun {
			if err := updateTransactionCategory(db, m, result.ID, result.Category); err != nil {
				fmt.Printf("✗ (%d/%d) ID %d DB update failed: %v\n", current, totalCount, result.ID, err)
				failed++
				continue
			}
		}

		fmt.Printf("✓ (%d/%d) ID %d: %s → %s\n",
			current, totalCount, result.ID, truncate(trans.Description, 45), result.Category)
		successful++
		categoryCount[result.Category]++
	}

	elapsed := time.Since(startTime)

	fmt.Println("\n" + strings.Repeat("=", 60))
	if *dryRun {
		fmt.Println("DRY RUN COMPLETE")
		fmt.Printf("Would classify: %d transactions\n", successful)
	} else {
		fmt.Println("CLASSIFICATION COMPLETE")
		fmt.Printf("Successfully classified: %d transactions\n", successful)
	}
	fmt.Printf("Failed: %d transactions\n", failed)
	fmt.Printf("Time elapsed: %v\n", elapsed.Round(time.Millisecond))
	if len(categoryCount) > 0 {
		fmt.Println("\nBy category:")
		for _, c := range m.Categories {
			if n := categoryCount[c.ID]; n > 0 {
				fmt.Printf("  %-24s %d\n", c.ID, n)
			}
		}
	}
}

func truncate(s string, maxLen int) string {
	if len(s) <= maxLen {
		return s
	}
	return s[:maxLen-3] + "..."
}
