<?php
error_reporting(0);
ini_set('display_errors', 0);
header("Content-Type: application/json");

$file = __DIR__ . '/movies.json';

// Initialize file if not exists
if (!file_exists($file)) {
    file_put_contents($file, json_encode([]));
    chmod($file, 0666);
}

function sendResponse($data) {
    echo json_encode($data);
    exit;
}

$method = $_SERVER['REQUEST_METHOD'];

function scrapeMovie($name) {
    $searchUrl = "https://www.google.com/search?q=" . urlencode($name . " terabox link");
    
    $ch = curl_init();
    curl_setopt($ch, CURLOPT_URL, $searchUrl);
    curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
    curl_setopt($ch, CURLOPT_FOLLOWLOCATION, true);
    curl_setopt($ch, CURLOPT_SSL_VERIFYPEER, false);
    curl_setopt($ch, CURLOPT_TIMEOUT, 5);
    curl_setopt($ch, CURLOPT_USERAGENT, 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36');
    
    $html = curl_exec($ch);
    curl_close($ch);

    if (!$html) return null;

    if (preg_match('/https?:\/\/(?:www\.)?(?:1024)?terabox\.(?:com|app|tech)\/s\/[a-zA-Z0-9_-]+/', $html, $matches)) {
        return $matches[0];
    }

    return null;
}

function normalize_key($name) {
    // lowercase, trim, replace spaces/underscores/hyphens
    $name = strtolower(trim($name));
    $name = preg_replace('/[\s_-]+/', '', $name);
    // ignore year/brackets like (2024) or [1080p]
    $name = preg_replace('/\(\d{4}\)|\[.*?\]/', '', $name);
    return $name;
}

if ($method === 'GET') {
    $search = isset($_GET['search']) ? trim($_GET['search']) : '';
    
    if (empty($search)) {
        sendResponse(['status' => 'error', 'message' => 'No search term provided']);
    }

    $normalized_search = normalize_key($search);
    $movies = json_decode(file_get_contents($file), true);
    if (!is_array($movies)) $movies = [];
    
    $best_match = null;
    $max_similarity = 0;

    foreach ($movies as $movie) {
        $stored_name = $movie['name'];
        $normalized_stored = normalize_key($stored_name);
        
        // Exact normalized match
        if ($normalized_stored === $normalized_search) {
            $movie['rating'] = $movie['rating'] ?? 0;
            $movie['votes'] = $movie['votes'] ?? 0;
            sendResponse(['status' => 'found', 'link' => $movie['link'] ?? '', 'name' => $stored_name, 'rating' => $movie['rating'], 'votes' => $movie['votes']]);
        }

        // Partial match
        if (strpos($normalized_stored, $normalized_search) !== false || strpos($normalized_search, $normalized_stored) !== false) {
            $best_match = $movie;
            continue; 
        }

        // Fuzzy matching (levenshtein)
        if (function_exists('levenshtein')) {
            $lev = levenshtein($normalized_search, $normalized_stored);
            $similarity = 1 - ($lev / max(strlen($normalized_search), strlen($normalized_stored), 1));
            if ($similarity > 0.8 && $similarity > $max_similarity) {
                $max_similarity = $similarity;
                $best_match = $movie;
            }
        }
    }

    if ($best_match) {
        $best_match['rating'] = $best_match['rating'] ?? 0;
        $best_match['votes'] = $best_match['votes'] ?? 0;
        sendResponse(['status' => 'found', 'link' => $best_match['link'] ?? '', 'name' => $best_match['name'], 'rating' => $best_match['rating'], 'votes' => $best_match['votes']]);
    }

    // AUTO SCRAP FALLBACK
    $scrapedLink = scrapeMovie($search);
    if ($scrapedLink) {
        $movies[] = ['name' => $search, 'link' => $scrapedLink];
        file_put_contents($file, json_encode($movies));
        sendResponse(['status' => 'found', 'link' => $scrapedLink]);
    }

    sendResponse(['status' => 'not_found']);

} elseif ($method === 'POST') {
    $action = $_POST['action'] ?? '';
    
    if ($action === 'add') {
        $name = $_POST['movie'] ?? '';
        $link = $_POST['link'] ?? '';
        
        if ($name && $link) {
            $movies = json_decode(file_get_contents($file), true);
            if (!is_array($movies)) $movies = [];
            
            // Check if exists
            foreach ($movies as $m) {
                if (strtolower(trim($m['name'])) === strtolower(trim($name))) {
                    sendResponse(['message' => 'Movie already exists']);
                }
            }
            
            $movies[] = ['name' => trim($name), 'link' => trim($link)];
            if (file_put_contents($file, json_encode($movies)) !== false) {
                sendResponse(['message' => 'Movie added successfully']);
            } else {
                sendResponse(['message' => 'Failed to write to file']);
            }
        } else {
            sendResponse(['message' => 'Movie name and link are required']);
        }
    } elseif ($action === 'rate') {
        $name = $_POST['movie'] ?? '';
        $rating = floatval($_POST['rating'] ?? 0);
        
        if ($name && $rating >= 1 && $rating <= 5) {
            $movies = json_decode(file_get_contents($file), true);
            if (!is_array($movies)) $movies = [];
            
            $found = false;
            foreach ($movies as &$m) {
                if (normalize_key($m['name']) === normalize_key($name)) {
                    $current_rating = $m['rating'] ?? 0;
                    $current_votes = $m['votes'] ?? 0;
                    
                    $new_votes = $current_votes + 1;
                    $new_rating = (($current_rating * $current_votes) + $rating) / $new_votes;
                    
                    $m['rating'] = round($new_rating, 1);
                    $m['votes'] = $new_votes;
                    $found = true;
                    break;
                }
            }
            
            if ($found) {
                file_put_contents($file, json_encode($movies));
                sendResponse(['message' => 'Rating submitted!', 'rating' => $m['rating'], 'votes' => $m['votes']]);
            } else {
                sendResponse(['message' => 'Movie not found']);
            }
        } else {
            sendResponse(['message' => 'Invalid data']);
        }
    } else {
        sendResponse(['message' => 'Invalid action']);
    }
}
?>
